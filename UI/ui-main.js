/* =========================================================================
 * 文件名: ui-main.js
 * 核心职责: 总控中心与事件总线 (连接 UI 与 后端 API)
 * 作用说明: 
 * 1. 监听所有与 Python 后端发生直接交互的按钮点击事件。
 * 2. 调度 ui-config.js 收集数据，并通过 bridge.js 发送给后端。
 * 3. 处理本地文件夹选择、历史文件导入、系统日志窗口调出等 API 调用。
 * ========================================================================= */

/**
 * Python 侧子窗口关闭（含原生 X）时通过 evaluate_js 调用，重置主界面按钮样式。
 * @param {'log'|'canary'} panel
 */
window.setPanelButtonInactive = function (panel) {
    const p = String(panel || '').toLowerCase();
    const id = p === 'canary' ? 'btn-show-canary' : 'btn-show-log';
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.setAttribute('variant', 'secondary');
    btn.setAttribute('treatment', 'outline');
};

document.addEventListener('DOMContentLoaded', () => {
    console.log(">> UI Main Controller Ready");

    // ==========================================
    // 1. 目录与文件选择器 (调用系统弹窗)
    // ==========================================
    const btnSelectPath = document.getElementById('btn-select-path');
    const inputSavePath = document.getElementById('input-save-path');
    const btnImportHistory = document.getElementById('btn-import-history');
    const inputHistoryPath = document.getElementById('input-history-path');

    if (btnSelectPath) {
        btnSelectPath.addEventListener('click', async () => {
            if (window.pywebview?.api) {
                try {
                    const path = await window.pywebview.api.select_folder();
                    if (path) inputSavePath.value = path;
                } catch (e) { console.error("文件夹选择失败:", e); }
            } else {
                alert("演示模式: 无法打开资源管理器");
            }
        });
    }

    if (btnImportHistory) {
        btnImportHistory.addEventListener('click', async () => {
            if (window.pywebview?.api) {
                try {
                    const path = await window.pywebview.api.select_file("json,txt,xlsx");
                    if (path) inputHistoryPath.value = path;
                } catch (e) { console.error("文件选择失败:", e); }
            } else {
                alert("演示模式: 无法打开资源管理器");
            }
        });
    }

    // ==========================================
    // 2. LOG 日志面板控制 + Canary（含悬停唤醒置顶）
    // ==========================================
    function isSatelliteButtonActive(btn) {
        if (!btn) return false;
        return btn.getAttribute('variant') === 'accent' && btn.getAttribute('treatment') === 'fill';
    }

    let hoverRaiseTimerLog = null;
    let hoverRaiseTimerCanary = null;

    const btnLog = document.getElementById('btn-show-log');

    if (btnLog) {
        btnLog.addEventListener('click', async () => {
            if (hoverRaiseTimerLog) {
                clearTimeout(hoverRaiseTimerLog);
                hoverRaiseTimerLog = null;
            }
            if (window.pywebview?.api) {
                try {
                    const isVisible = await window.pywebview.api.toggle_monitor_window();
                    btnLog.setAttribute('variant', isVisible ? 'accent' : 'secondary');
                    btnLog.setAttribute('treatment', isVisible ? 'fill' : 'outline');
                } catch (e) { console.error("LOG面板切换失败:", e); }
            }
        });
        btnLog.addEventListener('mouseenter', () => {
            if (!window.pywebview?.api || typeof window.pywebview.api.raise_monitor_window !== 'function') return;
            if (!isSatelliteButtonActive(btnLog)) return;
            if (hoverRaiseTimerLog) clearTimeout(hoverRaiseTimerLog);
            hoverRaiseTimerLog = setTimeout(() => {
                hoverRaiseTimerLog = null;
                try {
                    window.pywebview.api.raise_monitor_window();
                } catch (e) { console.error("LOG 悬停置顶失败:", e); }
            }, 350);
        });
        btnLog.addEventListener('mouseleave', () => {
            if (hoverRaiseTimerLog) {
                clearTimeout(hoverRaiseTimerLog);
                hoverRaiseTimerLog = null;
            }
        });
    }

    const btnCanary = document.getElementById('btn-show-canary');
    if (btnCanary) {
        btnCanary.addEventListener('click', async () => {
            if (hoverRaiseTimerCanary) {
                clearTimeout(hoverRaiseTimerCanary);
                hoverRaiseTimerCanary = null;
            }
            if (window.pywebview?.api) {
                try {
                    const isVisible = await window.pywebview.api.toggle_canary_window();
                    btnCanary.setAttribute('variant', isVisible ? 'accent' : 'secondary');
                    btnCanary.setAttribute('treatment', isVisible ? 'fill' : 'outline');
                } catch (e) { console.error("金丝雀窗口切换失败:", e); }
            }
        });
        btnCanary.addEventListener('mouseenter', () => {
            if (!window.pywebview?.api || typeof window.pywebview.api.raise_canary_window !== 'function') return;
            if (!isSatelliteButtonActive(btnCanary)) return;
            if (hoverRaiseTimerCanary) clearTimeout(hoverRaiseTimerCanary);
            hoverRaiseTimerCanary = setTimeout(() => {
                hoverRaiseTimerCanary = null;
                try {
                    window.pywebview.api.raise_canary_window();
                } catch (e) { console.error("Canary 悬停置顶失败:", e); }
            }, 350);
        });
        btnCanary.addEventListener('mouseleave', () => {
            if (hoverRaiseTimerCanary) {
                clearTimeout(hoverRaiseTimerCanary);
                hoverRaiseTimerCanary = null;
            }
        });
    }

    // ==========================================
    // 2b. 爬虫引擎 ↔ 设备伪装（stealth_engine）联动
    // ==========================================
    const engineSelect = document.getElementById('engine-select');
    const stealthEnginePicker = document.getElementById('stealth-engine');

    function syncStealthEnginePickerWithEngine() {
        if (!engineSelect || !stealthEnginePicker) return;
        const isBs = engineSelect.value === 'beautifulsoup';
        stealthEnginePicker.disabled = isBs;
        if (typeof stealthEnginePicker.toggleAttribute === 'function') {
            stealthEnginePicker.toggleAttribute('disabled', isBs);
        } else if (isBs) {
            stealthEnginePicker.setAttribute('disabled', '');
        } else {
            stealthEnginePicker.removeAttribute('disabled');
        }
    }

    if (engineSelect) {
        engineSelect.addEventListener('change', syncStealthEnginePickerWithEngine);
        syncStealthEnginePickerWithEngine();
    }

    // ==========================================
    // 3. 核心抓取控制 (严密校验 + 原生 Toast 版)
    // ==========================================
    const btnToggle = document.getElementById('btn-toggle-crawl');
    let isRunning = false;

    function syncCrawlUi(running) {
        isRunning = !!running;
        window.__crawlRunning = isRunning;
        if (!btnToggle) return;
        if (isRunning) {
            btnToggle.setAttribute('variant', 'negative');
            btnToggle.textContent = '停止爬取';
        } else {
            btnToggle.setAttribute('variant', 'cta');
            btnToggle.textContent = '自动爬虫';
        }
    }

    window.syncCrawlUiFromBackend = function (running) {
        syncCrawlUi(!!running);
    };

    // 唤起原生 Adobe Toast 的辅助函数
    function showBottomAlert(msg) {
        const toast = document.getElementById('sys-toast');
        if (toast) {
            toast.textContent = msg;         // 注入文本
            toast.setAttribute('open', '');  // 加上 open 属性，原生动画就会滑出来
            
            // 3.5秒后自动关闭
            setTimeout(() => { 
                toast.removeAttribute('open'); 
            }, 3500);
        }
    }

    // 监听原生 Toast 自带的 'X' 关闭按钮事件
    const sysToast = document.getElementById('sys-toast');
    if (sysToast) {
        sysToast.addEventListener('close', () => {
            sysToast.removeAttribute('open');
        });
    }

    if (btnToggle) {
        btnToggle.addEventListener('click', async () => {
            
            if (!isRunning) {
                // ====================
                // 启动前：严格的数据校验
                // ====================
                if (typeof collectUiConfig !== 'function') return;
                const config = collectUiConfig();

                const strategy = config.strategy_settings.crawl_strategy;
                // 🚀 [修复] 从新的数组 target_urls 中安全提取第一个网址用于 UI 校验
                const targetUrlsArray = config.strategy_settings.target_urls || [];
                const targetUrl = targetUrlsArray.length > 0 ? targetUrlsArray[0] : "";
                const searchKeyword = config.strategy_settings.search_keyword || "";

                // 🌟 严密的三重业务逻辑拦截 🌟
                if (strategy === 'full') {
                    if (!targetUrl) {
                        return showBottomAlert("全站历遍模式：请输入有效的目标网址！");
                    }
                } 
                else if (strategy === 'sitemap') {
                    // 必须包含 .xml 后缀，或者带有“_检索成功！”的标识，否则拦截
                    if (!targetUrl.includes('.xml') && !targetUrl.includes('_检索成功！')) {
                        return showBottomAlert("地图采集模式：请提供 .xml 地址，或先使用【智能检索】！");
                    }
                } 
                else if (['google_search', 'bing_search', 'duckduckgo'].includes(strategy)) {
                    // 关键词和网址必须至少填一个
                    if (!targetUrl && !searchKeyword) {
                        return showBottomAlert("搜索模式：请输入【搜索关键词】或附加【目标网址】！");
                    }
                }

                // ====================
                // 校验通过，开始启动
                // ====================
                console.log("🚀 [UI -> Python] 发送启动指令，最终配置:", config);
                
                syncCrawlUi(true);

                if (window.run_crawler) {
                    try {
                        await window.run_crawler(config);
                    } catch (error) {
                        console.error("Python 引擎启动失败:", error);
                        syncCrawlUi(false);
                    }
                }

            } else {
                // ====================
                // 停止逻辑
                // ====================
                console.log("🛑 [UI -> Python] 发送停止指令");
                if (window.stop_crawler) await window.stop_crawler();

                syncCrawlUi(false);
            }
        });
    }

// ==========================================
    // 4. 动态行交互 (LINK 按钮添加 与 垃圾桶删除) -> 从 V8 移植
    // ==========================================
    // 1. 获取动态视图的主容器和所有模式容器
    const viewContainer = document.querySelector('.dynamic-view-container');
    const viewFull = document.getElementById('view-full');
    const viewSitemap = document.getElementById('view-sitemap');
    const viewSearch = document.getElementById('view-search');

    // 2. 垃圾桶删除逻辑 (事件委托机制)
    if (viewContainer) {
        viewContainer.addEventListener('click', (e) => {
            // 如果点到的是垃圾桶图标或者它的外层 div
            const trashBtn = e.target.closest('.trash-btn') || e.target.closest('.square-btn');
            if (trashBtn) {
                const row = trashBtn.closest('.input-row');
                if (row) {
                    row.style.opacity = '0';
                    setTimeout(() => row.remove(), 200);
                }
            }
        });
    }

    // 3. Link 按钮添加新行逻辑
    // 放弃 V8 的模糊匹配，直接使用你 V9 定义的确切 ID
    const btnAddLink = document.getElementById('btn-show-link');
    
    if (btnAddLink) {
        btnAddLink.addEventListener('click', () => {
            // 判断当前是哪个视图处于激活状态
            let activeView = null;
            if (viewFull && !viewFull.classList.contains('hidden')) activeView = viewFull;
            else if (viewSearch && !viewSearch.classList.contains('hidden')) activeView = viewSearch;
            else if (viewSitemap && !viewSitemap.classList.contains('hidden')) activeView = viewSitemap;

            if (activeView) {
                const newRow = document.createElement('div');
                newRow.className = 'input-row';
                newRow.style.display = 'flex'; // 确保新行的 flex 布局生效
                newRow.style.marginTop = '8px'; // 增加一点间距

                const commonInput = `<sp-textfield placeholder="请输入附加网址..." style="flex-grow: 1;"></sp-textfield>`;
                // V9 风格的垃圾桶按钮
                const commonTrash = `<div class="square-btn trash-btn" style="cursor: pointer; margin-left: 8px; display: flex; align-items: center; justify-content: center;"><sp-icon-delete></sp-icon-delete></div>`;

                if (activeView === viewSitemap) {
                    // 地图采集模式特有结构 (保留 V8 的智能检索按钮位)
                    newRow.innerHTML = `${commonInput} <sp-button class="smart-btn" variant="secondary" style="margin-left: 8px;">智能检索</sp-button> ${commonTrash}`;
                } else {
                    // 标准结构
                    newRow.innerHTML = `${commonInput} ${commonTrash}`;
                }
                
                // 将新行追加到当前激活的视图中
                activeView.appendChild(newRow);
            } else {
                console.warn("[UI] 无法确定当前激活的视图，无法添加新行。");
            }
        });
    }
}); // 确保这是文件最末尾的闭合括号