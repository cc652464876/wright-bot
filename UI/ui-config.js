/* =========================================================================
 * 文件名: ui-config.js
 * 核心职责: 数据打包车间 (纯数据处理模块)
 * 作用说明: 
 * 1. 专职负责扫描界面上所有表单元素（下拉框、输入框、勾选框、滑块等）。
 * 2. 严格按照与 Python 后端约定的核心参数格式，将其组装成 JSON 结构。
 * 3. 本文件不包含任何按钮点击监听或界面特效，只提供 collectUiConfig() 方法供外界调用。
 * ========================================================================= */

/**
 * 核心引擎：收集 UI 上所有参数，严格打包成 Python 后端所需的模块化 JSON
 * @returns {Object} 包含 task_info, strategy_settings 等模块的配置对象
 */
function collectUiConfig() {
    // 1. 获取基础与策略选择
    const currentStrategy = document.getElementById('strategy-select').value;
    const engineType = document.getElementById('engine-select').value;
    const stealthPicker = document.getElementById('stealth-engine');
    const rawStealth = stealthPicker?.value;
    const stealthEngine =
        engineType === 'beautifulsoup'
            ? 'chromium'
            : (['chromium', 'rebrowser', 'camoufox'].includes(rawStealth) ? rawStealth : 'chromium');
    // Playwright 实际 browser_type（chromium/firefox/webkit）；与 stealth_engine 解耦，默认 Chromium
    const browserType = 'chromium';
    const concurrencyVal = document.getElementById('max-concurrency').value;
    
    // 2. 收集目标 URL 列表 (智能区分不同视图)
    let targetUrls = [];
    if (currentStrategy === 'full') {
        const view = document.getElementById('view-full');
        if(view) view.querySelectorAll('sp-textfield').forEach(inp => { if(inp.value.trim()) targetUrls.push(inp.value.trim()); });
    } else if (currentStrategy === 'sitemap') {
        const view = document.getElementById('view-sitemap');
        if(view) view.querySelectorAll('sp-textfield').forEach(inp => { if(inp.value.trim()) targetUrls.push(inp.value.trim()); });
    } else if (['google', 'bing', 'duckduckgo'].includes(currentStrategy)) {
        const view = document.getElementById('view-search');
        if(view) {
            // 精准跳过 API Key 和关键词输入框，只收集底部的附加目标网址
            const inputs = view.querySelectorAll('sp-textfield:not(#input-api-key):not(#input-search-keyword)');
            inputs.forEach(inp => { if(inp.value.trim()) targetUrls.push(inp.value.trim()); });
        }
    }

    // 3. 提取目标文件类型 (PDF/IMG/ALL)
    const fileTypeGroup = document.getElementById('file-type-selector');
    const selectedTypeBtn = fileTypeGroup ? fileTypeGroup.querySelector('[selected]') : null;
    const fileType = selectedTypeBtn ? selectedTypeBtn.getAttribute('value') : 'pdf';

    // === 🚀 新增：智能判断并计算 mode (site 还是 search) ===
    let currentMode = "site";
    if (['google', 'bing', 'duckduckgo'].includes(currentStrategy)) {
        currentMode = "search";
    }

    // 4. 组装终极模块化 JSON 配置 (对应 Python 端的 14 条核心参数)
    const config = {
        "task_info": {
            "mode": currentMode,  // <--- 🚀 根本解决办法：明确传递 mode 给后端总指挥
            "task_name": "PrismPDF_Task_" + new Date().getTime(),
            "save_directory": document.getElementById('input-save-path')?.value.trim() || "./downloads",
            "history_file": document.getElementById('input-history-path')?.value.trim() || "",
            "max_pdf_count": 50, // 假设默认抓取最大 50 个目标，或者日后从 UI 取值
            // 与 Python TaskInfoConfig.enable_realtime_jsonl_export 对应：站点线实时落盘 JSONL/TXT
            "enable_realtime_jsonl_export": document.getElementById('cb-realtime-jsonl-export')?.checked || false
        },
        "strategy_settings": {
            // 将前端下拉值映射为后端标准值
            "crawl_strategy": currentStrategy === 'google' ? 'google_search' : (currentStrategy === 'bing' ? 'bing_search' : currentStrategy),
            
            // 🚀 打补丁：直接移除单复数冲突的 target_url 和局限性强的 target_domain，统一使用 target_urls 数组传给后端
            "target_urls": targetUrls, 
            
            "search_keyword": document.getElementById('input-search-keyword')?.value.trim() || "",
            "api_key": document.getElementById('input-api-key')?.value.trim() || "",
            "file_type": fileType
        },
        "engine_settings": {
            "crawler_type": engineType,
            "browser_type": browserType,
            "link_strategy": "same-domain",
            "wait_until": "domcontentloaded"
        },
        "performance": {
            "max_concurrency": concurrencyVal, // "auto" 或 数字
            "min_concurrency": 1,
            "max_requests_per_crawl": 9999, // 可以日后做成 UI 设置
            "limit_rate": document.getElementById('cb-limit-rate')?.checked || false,
            "max_tasks_per_minute": 120
        },
        "timeouts_and_retries": {
            "request_handler_timeout_secs": 60,
            "navigation_timeout_secs": 30,
            "max_request_retries": 3
        },
        "stealth": {
            "headless": document.getElementById('cb-headless')?.checked || false,
            "use_fingerprint": document.getElementById('cb-stealth')?.checked || false,
            "ignore_ssl_error": document.getElementById('cb-ignore-ssl')?.checked || true,
            "stealth_engine": stealthEngine
        },
        // 前端本地使用的过滤条件 (供日后保存 Excel 和过滤文件大小使用)
        "ui_filters": {
            "save_excel": document.getElementById('cb-save-excel')?.checked || false,
            "save_log": document.getElementById('cb-save-log')?.checked || false,
            "min_file_size_mb": parseFloat(document.getElementById('i-file')?.value) || 0,
            "min_page_count": parseInt(document.getElementById('i-page')?.value) || 0,
            "min_img_size_mb": parseFloat(document.getElementById('i-img')?.value) || 0,
            "min_px": parseInt(document.getElementById('i-px')?.value) || 0
        }
    };

    return config;
}