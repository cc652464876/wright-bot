/* =========================================================================
 * 文件名: ui-status.js
 * 核心职责: 系统心跳与监护仪 (前后端状态同步) + 底部代理设置
 * 作用说明: 
 * 1. 控制底部状态栏的指示灯颜色 (绿: 就绪, 橙: 运行中, 红: 报错/未连接)。
 * 2. 核心功能: 启动 setInterval 定时器，每秒向 Python 后端请求当前爬虫状态。
 * 3. 容错处理: 根据后端真实状态，强制纠正前端“启动/停止”按钮的可用性和文本。
 * 4. 代理设置: 管理底部状态栏代理通道切换、正则校验及向后端推送代理配置。
 * ========================================================================= */

// ==========================================
// 新增：代理设置 UI 与交互逻辑模块
// ==========================================
function initProxySettingsUI() {
    const proxyPicker = document.getElementById('proxy-picker');
    const inputGroup = document.getElementById('proxy-input-group');
    const proxyInput = document.getElementById('proxy-input');
    const confirmBtn = document.getElementById('confirm-btn');
    const cancelBtn = document.getElementById('cancel-btn');

    // 防崩处理
    if (!proxyPicker || !inputGroup || !proxyInput || !confirmBtn || !cancelBtn) return;

    let savedProxyIP = "";
    let confirmedPickerValue = "A";

    function formatAndValidateProxy(value) {
        if (/^\d{1,5}$/.test(value)) {
            const port = parseInt(value, 10);
            if (port >= 1 && port <= 65535) return { valid: true, formattedIP: `127.0.0.1:${port}` };
        }
        const proxyRegex = /^([a-zA-Z0-9.-]+):(\d{1,5})$/;
        const match = value.match(proxyRegex);
        if (match) {
            const port = parseInt(match[2], 10);
            if (port >= 1 && port <= 65535) return { valid: true, formattedIP: value };
        }
        return { valid: false, formattedIP: value };
    }

    proxyInput.addEventListener('input', (event) => {
        const currentValue = event.target.value.trim();
        const validation = formatAndValidateProxy(currentValue);

        if (currentValue === "") {
            proxyInput.invalid = false;
            confirmBtn.disabled = true;
        } else if (validation.valid) {
            proxyInput.invalid = false;
            confirmBtn.disabled = false;
        } else {
            proxyInput.invalid = true;
            confirmBtn.disabled = true;
        }
    });

    proxyPicker.addEventListener('change', (event) => {
        proxyPicker.blur(); // 👈 【务必加上这行】强制失焦，撤销 Spectrum 的透明拦截网

        if (event.target.value === 'C') {
            inputGroup.classList.add('show');
            proxyInput.value = savedProxyIP;
            proxyInput.dispatchEvent(new Event('input'));
            setTimeout(() => proxyInput.focus(), 200); // 👈 【把 50 改成 200】给底层组件收回弹窗的缓冲时间
        } else {
            inputGroup.classList.remove('show');
            confirmedPickerValue = event.target.value;
            updateBackendProxy(event.target.value, "");
        }
    });

    confirmBtn.addEventListener('click', () => {
        const currentValue = proxyInput.value.trim();
        const validation = formatAndValidateProxy(currentValue);
        if (!validation.valid) return;

        proxyInput.value = validation.formattedIP;
        proxyInput.disabled = true;
        confirmBtn.disabled = true;
        cancelBtn.disabled = true;

        proxyPicker.pending = true;
        proxyPicker.style.pointerEvents = 'none';

        setTimeout(() => {
            savedProxyIP = validation.formattedIP;
            confirmedPickerValue = 'C';

            proxyPicker.pending = false;
            proxyPicker.style.pointerEvents = 'auto';
            proxyInput.disabled = false;
            cancelBtn.disabled = false;
            inputGroup.classList.remove('show');

            updateBackendProxy('C', savedProxyIP);
        }, 800);
    });

    cancelBtn.addEventListener('click', () => {
        inputGroup.classList.remove('show');
        proxyInput.invalid = false;
        proxyPicker.value = confirmedPickerValue;
    });

    // 预留给 Python 后端的通信口
    function updateBackendProxy(mode, ip) {
        console.log(`[Proxy] 代理模式已请求切换: 模式=${mode}, 节点=${ip || '无'}`);
        // 假设后端注入的 API 名为 update_proxy
        if (window.pywebview?.api && typeof window.pywebview.api.update_proxy === 'function') {
            window.pywebview.api.update_proxy(mode, ip);
        }
    }
}


// ==========================================
// 原有：心跳监护仪主逻辑
// ==========================================
document.addEventListener('DOMContentLoaded', () => {
    console.log(">> UI Status Monitor Started");

    // 1. 挂载代理设置 UI
    initProxySettingsUI();

    // 2. 状态栏心跳 UI 逻辑
    const heartbeatText = document.getElementById('heartbeat-text');
    const heartbeatIcon = document.getElementById('status-bug'); // 注意：根据你之前的HTML，这里可能是 status-bug 或者是 heartbeat-icon，我按原代码保留

    /**
     * 更新底部状态指示灯和文本
     * @param {string} state - 状态标识 ('running', 'idle', 'error', 'disconnected')
     */
    function updateStatusUI(state) {
        if (!heartbeatText || !heartbeatIcon) return;

        // 1. 清理：增加 bug-idle 到移除列表，移除不再使用的 dot-* 类名以保持整洁
        heartbeatIcon.classList.remove('bug-idle', 'bug-running', 'bug-error', 'bug-orange', 'bug-green', 'bug-red');

        // 2. 状态映射：JS 只负责描述“现在是什么状态”
        switch (state) {
            case 'running':
                heartbeatText.innerText = "采集中...";
                heartbeatIcon.classList.add('bug-running'); // 语义：运行中
                break;
            case 'idle':
                heartbeatText.innerText = "系统就绪";
                heartbeatIcon.classList.add('bug-idle');    // 语义：空闲/就绪
                break;
            case 'error':
            case 'disconnected':
                heartbeatText.innerText = "后端未连接";
                heartbeatIcon.classList.add('bug-error');   // 语义：错误
                break;
        }
    }

    // ==========================================
    // 核心心跳检测逻辑 (每秒执行一次)
    // ==========================================
    setInterval(async () => {
        const btnToggle = document.getElementById('btn-toggle-crawl');

        // 检查 PyWebview API 是否已经注入挂载
        if (window.pywebview?.api && typeof window.pywebview.api.get_status === 'function') {
            try {
                const status = await window.pywebview.api.get_status();

                if (status === 'running') {
                    if (typeof window.syncCrawlUiFromBackend === 'function') {
                        window.syncCrawlUiFromBackend(true);
                    } else if (btnToggle) {
                        btnToggle.setAttribute('variant', 'negative');
                        btnToggle.textContent = '停止爬取';
                    }
                    updateStatusUI('running');
                } else {
                    if (typeof window.syncCrawlUiFromBackend === 'function') {
                        window.syncCrawlUiFromBackend(false);
                    } else if (btnToggle) {
                        btnToggle.setAttribute('variant', 'cta');
                        btnToggle.textContent = '自动爬虫';
                    }
                    updateStatusUI('idle');
                }
            } catch (e) {
                updateStatusUI('error');
                if (typeof window.syncCrawlUiFromBackend === 'function') {
                    window.syncCrawlUiFromBackend(false);
                } else if (btnToggle) {
                    btnToggle.setAttribute('variant', 'cta');
                    btnToggle.textContent = '自动爬虫';
                }
            }
        } else {
            updateStatusUI('disconnected');
        }
    }, 1000);
});