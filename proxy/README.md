### CacheRoute Proxy

### 结构
proxy/<br>
&emsp;|proxy.py                   &emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp; # 业务平面(8001)：接收 scheduler 转发 → 选 instance → 转发<br>
&emsp;|sclient/                   &emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp; # 出站：proxy->scheduler 的注册/心跳/注销（你已经建了）<br>
&emsp;|&emsp;| scheduler_client.py<br>
&emsp;|resource/                   &emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;# 入站控制面(8002)：instance 池 + 控制接口<br>
&emsp;|&emsp;|instance_pool.py<br>
&emsp;|&emsp;|control_plane.py<br>
&emsp;|strategy/                   &emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;# proxy 内部的 instance 调度策略<br>
&emsp;|&emsp;|base.py<br>
&emsp;|&emsp;|round_robin.py<br>
&emsp;|&emsp;|least_inflight.py<br>
&emsp;|&emsp;|factory.py<br>

### 启动
```
python3 proxy/proxy_cli.py --<option>
```
支持argument形式，可选参数：<br>
`--cp-url`: Proxy 控制平面 URL（默认 http://127.0.0.1:8002）<br>
`--scheduler-cp-url`: Scheduler 控制平面 URL（默认 http://127.0.0.1:7002）<br>
`--proxy-id`： 当前 proxy_id（默认从环境变量 PROXY_ID 读取）<br>
`--scheduler-proxy-list-path`： Scheduler “代理列表”接口路径（默认 /v1/proxy/list)<br>
`--timeout`： HTTP 超时时间（默认 5s）<br>

支持进入后的REPL命令：
`:help`: 查看命令帮助<br>
`:status`: 查看 Proxy 控制平面健康状态与实例计数<br>
`:instances [N]`: 列出存活实例（默认 N=20）<br>
`:instances --all [N]`: 列出全部实例（包含 dead），默认 N=20<br>
`:watch [--all] [--interval S] [--limit N]`: 持续刷新（Ctrl+C 停止），用于观察 TTL/心跳是否稳定<br>
`:scheduler`: 查询 Scheduler 控制平面，看当前 proxy_id 是否已注册/在线<br>
`:exit/:quit`: 退出 REPL<br>

启动<br>
<img width="1200" height="125" alt="图片" src="https://github.com/user-attachments/assets/07b78380-bd7d-47ae-8f7d-f45cdd7882cb" />

命令集合<br>
<img width="1200" height="418" alt="图片" src="https://github.com/user-attachments/assets/0e161d8a-1321-436c-a78d-81feae125987" />

查看proxy-scheduler信息<br>
<img width="1200" height="184" alt="图片" src="https://github.com/user-attachments/assets/192ae569-d0ac-419c-b84f-db1c2a7a0f31" />

查看实例池<br>
<img width="1200" height="144" alt="图片" src="https://github.com/user-attachments/assets/183ccc5b-65dc-426d-843a-c8c1509fb7ab" />

