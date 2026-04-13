import os
import sys
import hashlib
import traceback
import asyncio
from datetime import datetime
from typing import Optional
from contextvars import ContextVar
from contextlib import asynccontextmanager

# ==========================================
# 模块 1：上下文变量声明 (空间折跃通道)
# ==========================================
current_registry = ContextVar('current_registry', default=None)
# [修复] 加上显式的类型注解，告诉 Pylance 这个变量允许存入字符串
current_error_dir: ContextVar[Optional[str]] = ContextVar('current_error_dir', default=None)


# ==========================================
# 模块 2：全局错误登记册
# ==========================================
class ErrorRegistry:
    def __init__(self):
        # 内存存储字典: { 'err_id': { 'type': str, 'location': str, 'first_time': str, 'message': str, 'urls': set(), 'count': int } }
        self.errors = {}

    def _generate_fingerprint(self, exc_val, exc_tb):
        """
        核心降噪算法：追踪 Traceback，仅使用“报错发生的代码位置”进行哈希去重
        彻底解决 Playwright 动态报错日志导致的去重失败问题。
        """
        tb_list = traceback.extract_tb(exc_tb)
        
        # 逆向遍历 traceback，找到最后一个属于本项目的文件（排除第三方库文件）
        target_frame = tb_list[-1] 
        for frame in reversed(tb_list):
            # 过滤掉 site-packages 和 playwright 内部抛出的深层异常，锁定到业务代码
            if "site-packages" not in frame.filename and "playwright" not in frame.filename:
                target_frame = frame
                break
                
        # 拼接指纹基础字符串: 异常类型|文件名:行号
        fingerprint_str = f"{type(exc_val).__name__}|{os.path.basename(target_frame.filename)}:{target_frame.lineno}"
        # 生成 MD5 取前 8 位作为全局 err_ID
        err_id = hashlib.md5(fingerprint_str.encode('utf-8')).hexdigest()[:8]
        
        return err_id, fingerprint_str

    def register_error(self, exc_val, exc_tb, url):
        err_id, location_str = self._generate_fingerprint(exc_val, exc_tb)
        
        if err_id not in self.errors:
            # 新错误：记录完整信息
            error_msg = str(exc_val)[:300] + ("..." if len(str(exc_val)) > 300 else "")
            self.errors[err_id] = {
                'type': type(exc_val).__name__,
                'location': location_str,
                'first_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'message': error_msg,
                'urls': {url} if url else set(),
                'count': 1
            }
            return {"is_new": True, "err_id": err_id}
        else:
            # 旧错误：增加计数，追加 URL
            self.errors[err_id]['count'] += 1
            # 内存保护：最多记录 50 个受影响的 URL
            if url and len(self.errors[err_id]['urls']) < 50:
                self.errors[err_id]['urls'].add(url)
            return {"is_new": False, "err_id": err_id}

    def export_to_markdown(self, filepath):
        total_errors = sum(err['count'] for err in self.errors.values())
        unique_errors = len(self.errors)
        
        lines = [
            f"# Crawlee + Playwright 全局报错聚合报告",
            f"**生成时间:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"**错误总数:** {total_errors} (合并降噪后: {unique_errors})",
            "---"
        ]
        
        for err_id, data in self.errors.items():
            lines.append(f"## [{err_id}] {data['type']}")
            lines.append(f"- **崩溃位置:** `{data['location']}`")
            lines.append(f"- **首次发生:** {data['first_time']}")
            lines.append(f"- **拦截总计:** {data['count']} 次")
            lines.append(f"- **首次报错摘要:**\n  ```text\n  {data['message']}\n  ```")
            lines.append(f"- **受影响的 URLs ({len(data['urls'])} 个示例):**")
            for u in list(data['urls']):
                lines.append(f"  - {u}")
            lines.append("---\n")
            
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))


# ==========================================
# 模块 3：贴身保镖拦截器 (Context Manager)
# ==========================================
@asynccontextmanager
async def error_interceptor(page, current_url="Unknown_URL"):
    """
    无感植入的底层拦截器。通过 contextvars 获取顶层实例。
    """
    registry = current_registry.get()
    error_dir = current_error_dir.get()
    
    if not registry or not error_dir:
        # 如果未处于我们设定的上下文中，直接放行不做拦截
        yield
        return
        
    try:
        yield
    except Exception as e:
        # 捕获异常并提取 traceback
        exc_type, exc_val, exc_tb = sys.exc_info()
        
        # 登记错误
        result = registry.register_error(exc_val, exc_tb, current_url)
        
        # 如果是首次发现的新错误，执行防御性快照
        if result["is_new"]:
            err_id = result["err_id"]
            try:
                # 使用独立的 5 秒超时保护，防止 TargetClosedError 导致二次崩溃
                async with asyncio.timeout(5.0):
                    screenshot_path = os.path.join(error_dir, f"{err_id}_screenshot.png")
                    html_path = os.path.join(error_dir, f"{err_id}_page.html")
                    await page.screenshot(path=screenshot_path)
                    content = await page.content()
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(content)
            except Exception as snapshot_e:
                print(f"[{err_id}] 警告: 错误快照保存失败 - {snapshot_e}")
                
        # 必须将原始异常重新抛出，交给 Crawlee 自身的 Retry 机制处理
        raise e