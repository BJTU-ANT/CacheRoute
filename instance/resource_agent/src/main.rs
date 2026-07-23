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

#[derive(Clone)]
struct GpuUtilSample { value: f64, ts_ms: u128 }

#[derive(Clone, Default)]
struct GpuUtilHistory { samples: Vec<GpuUtilSample> }

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


fn json_option_f64(value: Option<f64>) -> String {
    match value {
        Some(v) if v.is_finite() => format!("{:.3}", v),
        _ => "null".to_string(),
    }
}

fn json_option_u128(value: Option<u128>) -> String {
    match value {
        Some(v) => v.to_string(),
        None => "null".to_string(),
    }
}

fn parse_optional_gpu_value(raw: &str) -> (Option<f64>, bool, String, String) {
    let text = raw.trim();
    if text.is_empty() || text.eq_ignore_ascii_case("N/A") || text.eq_ignore_ascii_case("[N/A]") || text.eq_ignore_ascii_case("[Not Supported]") || text.eq_ignore_ascii_case("Not Supported") {
        return (None, false, "nvidia-smi".to_string(), text.to_string());
    }
    match text.parse::<f64>() {
        Ok(v) if v.is_finite() => (Some(v), true, "nvidia-smi".to_string(), text.to_string()),
        _ => (None, false, "nvidia-smi".to_string(), text.to_string()),
    }
}

fn rolling_stats(history: &mut GpuUtilHistory, value: Option<f64>, ts_ms: u128, window_ms: u128, max_samples: usize) -> (Option<f64>, Option<f64>, usize) {
    if let Some(v) = value {
        history.samples.push(GpuUtilSample { value: v, ts_ms });
    }
    history.samples.retain(|sample| ts_ms.saturating_sub(sample.ts_ms) <= window_ms);
    if history.samples.len() > max_samples {
        let drop_count = history.samples.len() - max_samples;
        history.samples.drain(0..drop_count);
    }
    if history.samples.is_empty() {
        return (None, None, 0);
    }
    let sum: f64 = history.samples.iter().map(|sample| sample.value).sum();
    let max = history.samples.iter().map(|sample| sample.value).fold(f64::NEG_INFINITY, f64::max);
    (Some(sum / history.samples.len() as f64), Some(max), history.samples.len())
}


fn gpu_error_json(error: &str, ts_ms: u128, window_ms: u128) -> String {
    format!(r#"[{{"index":null,"uuid":"unknown","name":"unknown GPU","utilization_pct":null,"utilization_pct_current":null,"utilization_pct_avg":null,"utilization_pct_max":null,"utilization_sample_count":0,"utilization_window_ms":{},"utilization_sample_ok":false,"utilization_source":"nvidia-smi","utilization_sample_timestamp_ms":{},"utilization_raw_value":null,"utilization_error":"{}","utilization_sample_quality":"command_error","memory_used_mb":null,"memory_total_mb":null,"memory_free_mb":null,"temperature_c":null,"power_w":null,"health":"error"}}]"#, window_ms, ts_ms, json_escape(error))
}

fn gpu_json(gpu_history: &mut std::collections::HashMap<String, GpuUtilHistory>, window_ms: u128, max_samples: usize) -> String {
    let ts_ms = now_ms();
    let out = Command::new("nvidia-smi")
        .args(["--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,memory.free,temperature.gpu,power.draw", "--format=csv,noheader,nounits"])
        .output();
    let out = match out {
        Ok(output) => output,
        Err(error) => {
            return gpu_error_json(&error.to_string(), ts_ms, window_ms);
        }
    };
    if !out.status.success() {
        return gpu_error_json(&String::from_utf8_lossy(&out.stderr), ts_ms, window_ms);
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let mut rows = Vec::new();
    for line in text.lines() {
        let cols: Vec<&str> = line.split(',').map(|x| x.trim()).collect();
        if cols.len() < 9 { continue; }
        let uuid = cols[1].to_string();
        let (current, ok, source, raw_value) = parse_optional_gpu_value(cols[3]);
        let history = gpu_history.entry(uuid.clone()).or_default();
        let (avg, max, sample_count) = rolling_stats(history, current, ts_ms, window_ms, max_samples);
        let quality = if ok { "ok" } else { "invalid" };
        let error = if ok { "null".to_string() } else { format!("\"invalid utilization.gpu: {}\"", json_escape(&raw_value)) };
        rows.push(format!(
            "{{\"index\":{},\"uuid\":\"{}\",\"name\":\"{}\",\"utilization_pct\":{},\"utilization_pct_current\":{},\"utilization_pct_avg\":{},\"utilization_pct_max\":{},\"utilization_sample_count\":{},\"utilization_window_ms\":{},\"utilization_sample_ok\":{},\"utilization_source\":\"{}\",\"utilization_sample_timestamp_ms\":{},\"utilization_raw_value\":\"{}\",\"utilization_error\":{},\"utilization_sample_quality\":\"{}\",\"memory_used_mb\":{},\"memory_total_mb\":{},\"memory_free_mb\":{},\"temperature_c\":{},\"power_w\":{},\"health\":\"ok\"}}",
            cols[0].parse::<u32>().unwrap_or(0), json_escape(cols[1]), json_escape(cols[2]), json_option_f64(avg), json_option_f64(current), json_option_f64(avg), json_option_f64(max), sample_count, window_ms, ok, json_escape(&source), json_option_u128(Some(ts_ms)), json_escape(&raw_value), error, quality, json_option_f64(parse_optional_gpu_value(cols[4]).0), json_option_f64(parse_optional_gpu_value(cols[5]).0), json_option_f64(parse_optional_gpu_value(cols[6]).0), json_option_f64(parse_optional_gpu_value(cols[7]).0), json_option_f64(parse_optional_gpu_value(cols[8]).0)
        ));
    }
    format!("[{}]", rows.join(","))
}

fn build_snapshot(instance_id: &str, prev_cpu: &mut Option<(u64, u64)>, prev_net: &mut Option<NetSample>, gpu_history: &mut std::collections::HashMap<String, GpuUtilHistory>, gpu_window_ms: u128, gpu_max_samples: usize) -> String {
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
    let gpu = gpu_json(gpu_history, gpu_window_ms, gpu_max_samples);
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
    let mut gpu_window_ms = 5000_u128;
    let mut gpu_max_samples = 20_usize;
    let mut instance_id = env::var("INSTANCE_ID").unwrap_or_else(|_| "unknown".to_string());
    let args: Vec<String> = env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--listen" if i + 1 < args.len() => { listen = args[i + 1].clone(); i += 1; }
            "--sample-interval-ms" if i + 1 < args.len() => { interval_ms = args[i + 1].parse().unwrap_or(1000); i += 1; }
            "--instance-id" if i + 1 < args.len() => { instance_id = args[i + 1].clone(); i += 1; }
            "--gpu-util-window-ms" if i + 1 < args.len() => { gpu_window_ms = args[i + 1].parse().unwrap_or(5000); i += 1; }
            "--gpu-util-max-samples" if i + 1 < args.len() => { gpu_max_samples = args[i + 1].parse().unwrap_or(20); i += 1; }
            _ => {}
        }
        i += 1;
    }

    let state = Arc::new(Mutex::new(AgentState::default()));
    let sampler_state = state.clone();
    thread::spawn(move || {
        let mut prev_cpu = None;
        let mut prev_net = None;
        let mut gpu_history: std::collections::HashMap<String, GpuUtilHistory> = std::collections::HashMap::new();
        loop {
            let snapshot = build_snapshot(&instance_id, &mut prev_cpu, &mut prev_net, &mut gpu_history, gpu_window_ms, gpu_max_samples);
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strict_gpu_util_parsing() {
        assert_eq!(parse_optional_gpu_value("42").0, Some(42.0));
        assert_eq!(parse_optional_gpu_value("0").0, Some(0.0));
        assert_eq!(parse_optional_gpu_value("N/A").0, None);
        assert_eq!(parse_optional_gpu_value("[Not Supported]").0, None);
        assert_eq!(parse_optional_gpu_value("bad").0, None);
        let error = gpu_error_json("missing binary", 100, 5000);
        assert!(error.contains("utilization_error"));
        assert!(error.contains("command_error"));
    }

    #[test]
    fn rolling_window_stats_are_bounded() {
        let mut history = GpuUtilHistory::default();
        let (avg, max, count) = rolling_stats(&mut history, Some(10.0), 1000, 1000, 3);
        assert_eq!(avg, Some(10.0));
        assert_eq!(max, Some(10.0));
        assert_eq!(count, 1);
        rolling_stats(&mut history, Some(30.0), 1200, 1000, 3);
        let (avg, max, count) = rolling_stats(&mut history, Some(50.0), 1400, 1000, 3);
        assert_eq!(avg, Some(30.0));
        assert_eq!(max, Some(50.0));
        assert_eq!(count, 3);
        let (_, _, count) = rolling_stats(&mut history, Some(70.0), 1600, 1000, 3);
        assert_eq!(count, 3);
        let (avg, max, count) = rolling_stats(&mut history, None, 3000, 1000, 3);
        assert_eq!(avg, None);
        assert_eq!(max, None);
        assert_eq!(count, 0);
    }
}
