# ==========================================
# net_monitor.py - 核心网络监护仪 (HTTP代理同步版)
# ==========================================
import time
import threading

class NetworkMonitor:
    def __init__(self, window=None):
        self.window = window
        self._running = False
        self._tick = 0 
        
        # 保存来自前端 UI 的代理状态，默认 A (自动/跟随系统VPN)
        self.proxy_mode = "A" 
        self.proxy_ip = ""

    def update_proxy_settings(self, mode, ip=""):
        """接收来自前端的主动代理切换"""
        self.proxy_mode = mode
        self.proxy_ip = ip
        print(f"[NetMonitor] 代理状态同步: 模式={mode}, 节点={ip}")

    def _check_http(self, url, timeout=2.0):
        """企业级探活：返回延迟(毫秒)，超时或失败返回 -1"""
        try:
            # 👈 懒加载：仅在实际执行网络请求时才导入重型库
            import urllib.request 

            handlers = []
            if self.proxy_mode == "B": 
                handlers.append(urllib.request.ProxyHandler({}))
            elif self.proxy_mode == "C" and self.proxy_ip:
                handlers.append(urllib.request.ProxyHandler({
                    'http': f'http://{self.proxy_ip}',
                    'https': f'http://{self.proxy_ip}'
                }))

            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            
            start_time = time.time() # 👈 记录发包时间
            
            with opener.open(req, timeout=timeout) as response:
                if response.getcode() in [200, 204]:
                    # 👈 计算耗时并转为整数毫秒
                    return int((time.time() - start_time) * 1000) 
            return -1
        except Exception:
            return -1

    def _update_status(self):
        """后台同时检测国内和海外连通性"""
        cn_ping = self._check_http("http://www.baidu.com", timeout=1.5)
        glb_ping = self._check_http("http://www.google.com/generate_204", timeout=2)
        
        if self.window:
            try:
                # 👈 现在传给前端的是具体的毫秒数字，而不是 true/false 了
                js_code = f"if(window.updateNetStatusUI) window.updateNetStatusUI({cn_ping}, {glb_ping});"
                self.window.evaluate_js(js_code)
            except Exception:
                pass

    def _monitor_loop(self):
        print("[Thread] 🚀 系统网速监控循环已进入工作状态")
        try:
            # 👈 懒加载：仅在监控线程真正跑起来时，才去调用底层 C API 导入 psutil
            import psutil 
            last_io = psutil.net_io_counters()
        except Exception as e:
            print(f"[Thread] ❌ 初始获取网卡失败: {e}")
            return

        while self._running:
            time.sleep(1)
            try:
                # 注意：因为上面已经 import 过，这里的 psutil 可以正常使用，且 Python 会走缓存，不会重复加载
                current_io = psutil.net_io_counters()
                up_speed = (current_io.bytes_sent - last_io.bytes_sent) / 1024
                down_speed = (current_io.bytes_recv - last_io.bytes_recv) / 1024
                last_io = current_io
                
                if self.window:
                    js_code = f"if(window.updateNetSpeedUI) window.updateNetSpeedUI({up_speed:.1f}, {down_speed:.1f});"
                    self.window.evaluate_js(js_code)
                
                self._tick += 1
                if self._tick >= 3:
                    self._tick = 0
                    threading.Thread(target=self._update_status, daemon=True).start()
            except Exception:
                pass

    def start(self, window=None):
        if window:
            self.window = window
        if not self._running:
            self._running = True
            threading.Thread(target=self._monitor_loop, daemon=True, name="SysNetMonitor").start()
            print("[INFO] ✅ 系统网速监控已启动")

    def stop(self):
        self._running = False