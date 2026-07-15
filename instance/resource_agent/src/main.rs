use std::env;
use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[derive(Clone, Default)]
struct NetSample {
    rx_bytes: u64,
    tx_bytes: u64,
    ts_ms: u128,
}

#[derive(Clone, Default)]
struct AgentState {
    snapshot: String,
}

fn now_ms() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis()
}

fn json_escape(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"").replace('\n', " ")
}

fn read_meminfo() -> (u64, u64, u64) {
    let raw = fs::read_to_string("/proc/meminfo").unwrap_or_default();
    let mut total_kb: u64 = 0;
    let mut avail_kb: u64 = 0;
    for line in raw.lines() {
        let mut parts = line.split_whitespace();
        match parts.next() {
            Some("MemTotal:") => total_kb = parts.next().unwrap_or("0").parse().unwrap_or(0),
            Some("MemAvailable:") => avail_kb = parts.next().unwrap_or("0").parse().unwrap_or(0),
            _ => {}
        }
    }
    let total_mb = total_kb / 1024;
    let free_mb = avail_kb / 1024;
    let used_mb = total_mb.saturating_sub(free_mb);
    (used_mb, total_mb, free_mb)
}

fn read_loadavg() -> (f64, f64, f64) {
    let raw = fs::read_to_string("/proc/loadavg").unwrap_or_default();
    let vals: Vec<f64> = raw.split_whitespace().take(3).map(|x| x.parse().unwrap_or(0.0)).collect();
    (*vals.get(0).unwrap_or(&0.0), *vals.get(1).unwrap_or(&0.0), *vals.get(2).unwrap_or(&0.0))
}

fn read_cpu_jiffies() -> Option<(u64, u64)> {
    let raw = fs::read_to_string("/proc/stat").ok()?;
    let line = raw.lines().next()?;
    let nums: Vec<u64> = line.split_whitespace().skip(1).map(|x| x.parse().unwrap_or(0)).collect();
    let idle = nums.get(3).unwrap_or(&0) + nums.get(4).unwrap_or(&0);
    let total: u64 = nums.iter().sum();
    Some((idle, total))
}

fn read_network(prev: &mut Option<NetSample>) -> (String, f64, f64, Option<u64>) {
    let raw = fs::read_to_string("/proc/net/dev").unwrap_or_default();
    let mut best_iface = String::from("unknown");
    let mut rx = 0_u64;
    let mut tx = 0_u64;
    for line in raw.lines().skip(2) {
        let trimmed = line.trim();
        if trimmed.starts_with("lo:") { continue; }
        let parts: Vec<&str> = trimmed.split(|c| c == ':' || c == ' ').filter(|s| !s.is_empty()).collect();
        if parts.len() >= 17 {
            best_iface = parts[0].to_string();
            rx = parts[1].parse().unwrap_or(0);
            tx = parts[9].parse().unwrap_or(0);
            break;
        }
    }
    let ts = now_ms();
    let mut rx_mbps = 0.0;
    let mut tx_mbps = 0.0;
    if let Some(old) = prev.clone() {
        let dt_ms = ts.saturating_sub(old.ts_ms) as f64;
        if dt_ms > 0.0 {
            rx_mbps = rx.saturating_sub(old.rx_bytes) as f64 * 8.0 / dt_ms / 1000.0;
            tx_mbps = tx.saturating_sub(old.tx_bytes) as f64 * 8.0 / dt_ms / 1000.0;
        }
    }
    *prev = Some(NetSample { rx_bytes: rx, tx_bytes: tx, ts_ms: ts });
    let speed = fs::read_to_string(format!("/sys/class/net/{}/speed", best_iface)).ok().and_then(|s| s.trim().parse().ok());
    (best_iface, rx_mbps, tx_mbps, speed)
}

fn gpu_json() -> String {
    let out = Command::new("nvidia-smi")
        .args(["--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,memory.free,temperature.gpu,power.draw", "--format=csv,noheader,nounits"])
        .output();
    let Ok(out) = out else { return "[]".to_string(); };
    if !out.status.success() { return "[]".to_string(); }
    let text = String::from_utf8_lossy(&out.stdout);
    let mut rows = Vec::new();
    for line in text.lines() {
        let cols: Vec<&str> = line.split(',').map(|x| x.trim()).collect();
        if cols.len() < 9 { continue; }
        rows.push(format!(
            "{{\"index\":{},\"uuid\":\"{}\",\"name\":\"{}\",\"utilization_pct\":{},\"memory_used_mb\":{},\"memory_total_mb\":{},\"memory_free_mb\":{},\"temperature_c\":{},\"power_w\":{},\"health\":\"ok\"}}",
            cols[0].parse::<u32>().unwrap_or(0), json_escape(cols[1]), json_escape(cols[2]), cols[3], cols[4], cols[5], cols[6], cols[7], cols[8].replace("[Not Supported]", "0")
        ));
    }
    format!("[{}]", rows.join(","))
}

fn build_snapshot(instance_id: &str, prev_cpu: &mut Option<(u64, u64)>, prev_net: &mut Option<NetSample>) -> String {
    let ts = now_ms();
    let (used_mb, total_mb, free_mb) = read_meminfo();
    let (load1, load5, load15) = read_loadavg();
    let cpu_now = read_cpu_jiffies();
    let mut cpu_util = 0.0;
    if let (Some((old_idle, old_total)), Some((idle, total))) = (*prev_cpu, cpu_now) {
        let total_delta = total.saturating_sub(old_total);
        let idle_delta = idle.saturating_sub(old_idle);
        if total_delta > 0 { cpu_util = 100.0 * (total_delta.saturating_sub(idle_delta)) as f64 / total_delta as f64; }
    }
    if let Some(v) = cpu_now { *prev_cpu = Some(v); }
    let (iface, rx_mbps, tx_mbps, speed) = read_network(prev_net);
    let gpu = gpu_json();
    let mem_free_ratio = if total_mb > 0 { free_mb as f64 / total_mb as f64 } else { 0.0 };
    let admission = if mem_free_ratio < 0.05 { "rejecting" } else if cpu_util > 95.0 { "degraded" } else { "accepting" };
    format!(
        "{{\"schema_version\":\"resource_snapshot_v1\",\"agent_version\":\"0.1.0\",\"instance_id\":\"{}\",\"timestamp_ms\":{},\"devices\":{{\"gpu\":{},\"cpu\":{{\"utilization_pct\":{:.3},\"load1\":{:.3},\"load5\":{:.3},\"load15\":{:.3}}},\"memory\":{{\"used_mb\":{},\"total_mb\":{},\"free_mb\":{}}},\"network\":[{{\"iface\":\"{}\",\"rx_mbps\":{:.3},\"tx_mbps\":{:.3},\"speed_mbps\":{}}}]}},\"runtime\":{{}},\"capacity_hint\":{{\"memory_free_ratio\":{:.4},\"admission_state\":\"{}\"}}}}",
        json_escape(instance_id), ts, gpu, cpu_util, load1, load5, load15, used_mb, total_mb, free_mb, json_escape(&iface), rx_mbps, tx_mbps, speed.unwrap_or(0), mem_free_ratio, admission
    )
}

fn respond(mut stream: TcpStream, status: &str, content_type: &str, body: &str) {
    let resp = format!("HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", status, content_type, body.len(), body);
    let _ = stream.write_all(resp.as_bytes());
}

fn handle_client(mut stream: TcpStream, state: Arc<Mutex<AgentState>>) {
    let mut buf = [0; 1024];
    let n = stream.read(&mut buf).unwrap_or(0);
    let req = String::from_utf8_lossy(&buf[..n]);
    let path = req.split_whitespace().nth(1).unwrap_or("/");
    match path {
        "/healthz" => respond(stream, "200 OK", "application/json", "{\"ok\":true}"),
        "/v1/resource/snapshot" => {
            let body = state.lock().unwrap().snapshot.clone();
            respond(stream, "200 OK", "application/json", &body);
        }
        _ => respond(stream, "404 Not Found", "application/json", "{\"error\":\"not_found\"}"),
    }
}

fn main() {
    let mut listen = String::from("127.0.0.1:9101");
    let mut interval_ms = 1000_u64;
    let mut instance_id = env::var("INSTANCE_ID").unwrap_or_else(|_| "unknown".to_string());
    let args: Vec<String> = env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--listen" if i + 1 < args.len() => { listen = args[i + 1].clone(); i += 1; }
            "--sample-interval-ms" if i + 1 < args.len() => { interval_ms = args[i + 1].parse().unwrap_or(1000); i += 1; }
            "--instance-id" if i + 1 < args.len() => { instance_id = args[i + 1].clone(); i += 1; }
            _ => {}
        }
        i += 1;
    }

    let state = Arc::new(Mutex::new(AgentState::default()));
    let sampler_state = state.clone();
    thread::spawn(move || {
        let mut prev_cpu = None;
        let mut prev_net = None;
        loop {
            let snapshot = build_snapshot(&instance_id, &mut prev_cpu, &mut prev_net);
            sampler_state.lock().unwrap().snapshot = snapshot;
            thread::sleep(Duration::from_millis(interval_ms));
        }
    });

    thread::sleep(Duration::from_millis(50));
    let listener = TcpListener::bind(&listen).unwrap_or_else(|e| panic!("bind {} failed: {}", listen, e));
    eprintln!("[ResourceAgent] listening on http://{}", listen);
    for stream in listener.incoming().flatten() {
        let st = state.clone();
        thread::spawn(move || handle_client(stream, st));
    }
}
