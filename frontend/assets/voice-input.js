/**
 * ZYVoice —— 浏览器原生语音输入模块（Web Speech API）
 * =====================================================
 * 给「中渔小助」AI 客服加语音输入：点麦克风说话 → 文字实时回填输入框 →
 * 用户确认后再发送。
 *
 * 为什么是"回填输入框"而不是"直接发送"
 * --------------------------------------
 * 这个 Agent 能**真实下单、查订单、发起退货**。语音识别把「红虫颗粒」听成
 * 「红虫颗粒儿」或别的名字，若识别完直接发送，等于让用户为一次误识别背单。
 * 所以这里只负责"把语音变成可编辑的文字"，**发送与否永远由用户决定**。
 *
 * 为什么用浏览器原生 API 而不是后端 ASR
 * --------------------------------------
 * 零配置、零成本、零密钥，本地与演示环境开箱即用；识别在浏览器侧完成，
 * 后端一行代码都不用改。若将来要换成云端 ASR（腾讯云/讯飞等），只需替换本文件
 * 的 `recognizeOnce()` 实现，上层调用方无需改动。
 *
 * 已知限制（会在界面上明确告知，而不是静默失败）
 * ----------------------------------------------
 * 1. 仅 Chromium 内核（Chrome / Edge / 新版 Safari）支持；Firefox 不支持。
 * 2. 需要**安全上下文**：https 或 localhost。用局域网 IP 以 http 打开会被浏览器拦截。
 * 3. Chrome 的识别过程会把音频送到浏览器厂商的服务器，需联网；
 *    首次使用需授权麦克风。
 *
 * 用法
 * ----
 *     const voice = ZYVoice.create({
 *         lang: 'zh-CN',
 *         getText:  () => input.value,        // 取输入框当前文字
 *         setText:  (t) => { input.value = t; },
 *         onState:  (state, detail) => {},    // idle/starting/listening/…
 *         onNotice: (msg, level) => {},       // 需要给用户看的提示
 *     });
 *     voice.toggle();      // 点一下开始 / 再点一下停止
 */
(function (global) {
    'use strict';

    /** 浏览器是否提供语音识别 */
    function rawRecognition() {
        return global.SpeechRecognition || global.webkitSpeechRecognition || null;
    }

    /**
     * 不支持时给出**可操作**的原因，而不是笼统地说"不支持"。
     * 返回 null 表示可用；返回字符串表示不可用原因（面向用户）。
     */
    function unsupportedReason() {
        if (!rawRecognition()) {
            return '当前浏览器不支持语音识别，请换用 Chrome 或 Edge。';
        }
        // file:// 或局域网 http 下浏览器会禁用麦克风；提前告知，比点击后报错友好
        var loc = global.location || {};
        var protocol = loc.protocol || 'http:';
        var host = loc.hostname || '';
        var isLocalhost = host === 'localhost' || host === '127.0.0.1'
            || host === '::1' || host === '';
        var isHttpLike = protocol === 'http:' || protocol === 'https:' || protocol === 'file:';

        if (!isHttpLike) {
            return '当前页面协议（' + protocol + '）无法使用麦克风，请用 http://localhost:8001 打开。';
        }
        // 浏览器给出的权威判断（file:// 下 isSecureContext 为 false）
        if (global.isSecureContext === false && !isLocalhost) {
            return '浏览器只在 https 或 localhost 下允许使用麦克风；'
                + '请用 http://localhost:8001（或 https 域名）访问后再试。';
        }
        if (host && !isLocalhost && protocol === 'http:') {
            return '浏览器只在 https 或 localhost 下允许使用麦克风；'
                + '当前是局域网地址（' + host + '），请改用 http://localhost:8001 打开。';
        }
        return null;
    }

    /** 语音识别错误码 → 面向用户的可操作提示 */
    function explainError(code) {
        switch (code) {
            case 'not-allowed':
            case 'service-not-allowed':
                return '麦克风权限被拒绝。请点击地址栏左侧的权限图标，把「麦克风」改为允许后重试。';
            case 'audio-capture':
                return '没有检测到麦克风设备，请检查电脑是否已连接麦克风。';
            case 'no-speech':
                return '没有听到声音，靠近麦克风再试一次。';
            case 'network':
                return '语音识别需要联网（浏览器会把音频送到厂商服务器），请检查网络。';
            case 'aborted':
                return '';                       // 主动中止，不提示
            default:
                return '语音识别出错（' + code + '），请改用文字输入。';
        }
    }

    function create(options) {
        var opt = options || {};
        var lang = opt.lang || 'zh-CN';
        var maxSeconds = opt.maxSeconds || 60;      // 单次最长录音，防"忘记关"一直挂着
        var restartGap = 320;                       // Chrome 静音会自动 onend，这里接着续上

        var rec = null;
        var state = 'idle';          // idle | starting | listening | stopped | denied | error
        var baseText = '';           // 识别开始前输入框里的原有文字
        var segments = [];           // 本次会话识别到的语音片段（按 results 索引存）
        var startedAt = 0;
        var timer = null;
        var restartTimer = null;
        var wantListening = false;   // 用户意图：希望处于"正在听"状态

        // ── 回调（缺省为空函数，调用方不必层层判空）──
        function getText() { return opt.getText ? (opt.getText() || '') : ''; }
        function setText(t) { if (opt.setText) opt.setText(t); }
        function emitState(s, detail) { if (opt.onState) opt.onState(s, detail || ''); }
        function notice(msg, level) { if (msg && opt.onNotice) opt.onNotice(msg, level || 'info'); }

        function clearTimers() {
            if (timer) { clearTimeout(timer); timer = null; }
            if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
        }

        /**
         * 拼接语音片段 —— 两个必须处理的细节：
         *
         * 1) **段间不能一律加空格**：用户说「红虫颗粒」「还有货吗」是连续一句话，
         *    加空格读起来像两段。但中英文/数字混说时又必须断开
         *    （"SKU" + "123" 不能粘成 "SKU123"）。故按边界字符判断。
         * 2) **同一段从 interim 转 final 时不能重复累加**：浏览器会带着累积的
         *    results 再发一次事件，若用 `final += txt`，界面上的字会在识别结束
         *    的瞬间发生变化（空格消失，肉眼可见地在跳）。所以按 results 索引
         *    **就地覆盖** —— 同一条语音无论 interim 还是 final，显示完全一致。
         */
        function isCJK(ch) { return !!ch && /[\u3400-\u9FAF\uF900-\uFAFF]/.test(ch); }

        function cat(a, b) {
            if (!a) return b || '';
            if (!b) return a;
            var x = a.charAt(a.length - 1);
            var y = b.charAt(0);
            return (isCJK(x) && isCJK(y)) ? a + b : a + ' ' + b;
        }

        function speechSoFar() {
            var out = '';
            for (var i = 0; i < segments.length; i++) {
                if (segments[i]) out = cat(out, segments[i]);
            }
            return out;
        }

        function renderText() {
            var speech = speechSoFar();
            var head = baseText ? baseText.replace(/\s+$/, '') : '';
            if (head && speech) return head + ' ' + speech;
            return head || speech;
        }

        function build() {
            var SR = rawRecognition();
            rec = new SR();
            rec.lang = lang;
            rec.continuous = true;        // 允许连续说，不必一句一点
            rec.interimResults = true;    // 边说边出字，让用户看到识别进度
            rec.maxAlternatives = 1;

            rec.onresult = function (e) {
                // results 是累积的：只有从 resultIndex 起才是本次新增/更新的段落
                for (var i = e.resultIndex; i < e.results.length; i++) {
                    var r = e.results[i];
                    var txt = r && r[0] ? r[0].transcript : '';
                    if (txt) segments[i] = txt;      // 就地覆盖，interim→final 不重复累加
                }
                // 用户原本写的内容 + 语音内容，两者之间始终留一个空格分隔
                setText(renderText());
            };

            rec.onerror = function (e) {
                var code = (e && e.error) || 'unknown';
                if (code === 'no-speech') {
                    // 没说上话不算失败：留在监听态，等用户继续说
                    notice(explainError(code), 'warn');
                    return;
                }
                wantListening = false;
                clearTimers();
                var msg = explainError(code);
                if (code === 'not-allowed' || code === 'service-not-allowed') {
                    state = 'denied';
                    emitState('denied', code);
                } else if (code !== 'aborted') {
                    state = 'error';
                    emitState('error', code);
                }
                if (msg) notice(msg, 'error');
            };

            rec.onend = function () {
                clearTimers();
                if (!wantListening) {
                    state = 'idle';
                    emitState('idle', 'user-stop');
                    return;
                }
                // Chrome 静音约 8 秒会自动结束；用户还想继续说 → 隔一下自动续上
                if (Date.now() - startedAt > maxSeconds * 1000) {
                    wantListening = false;
                    state = 'idle';
                    emitState('idle', 'max-duration');
                    notice('已达到单次 ' + maxSeconds + ' 秒上限，按发送继续。', 'info');
                    return;
                }
                restartTimer = setTimeout(function () {
                    if (!wantListening) return;
                    // ⚠️ 续听开启的是**新的**识别会话，其 results 从索引 0 重新计数。
                    // 若不先把已识别文字固化进前缀，新会话的第 0 段就会覆盖掉旧内容。
                    var spoken = speechSoFar();
                    if (spoken) {
                        baseText = baseText ? baseText.replace(/\s+$/, '') + ' ' + spoken : spoken;
                        segments = [];
                        setText(renderText());
                    }
                    try { rec.start(); } catch (err) { /* 已在运行则忽略 */ }
                }, restartGap);
            };

            rec.onstart = function () {
                state = 'listening';
                emitState('listening', '');
            };
        }

        function start() {
            var reason = unsupportedReason();
            if (reason) {
                state = 'error';
                emitState('unsupported', reason);
                notice(reason, 'error');
                return false;
            }
            if (state === 'listening' || state === 'starting') return true;

            baseText = getText();
            segments = [];
            wantListening = true;
            state = 'starting';
            emitState('starting', '');
            clearTimers();
            build();
            try {
                rec.start();
            } catch (e) {
                state = 'error';
                emitState('error', 'start-failed');
                notice('无法启动语音识别：' + (e && e.message ? e.message : e), 'error');
                return false;
            }
            startedAt = Date.now();
            timer = setTimeout(function () {
                if (wantListening) stop();
            }, maxSeconds * 1000);
            return true;
        }

        /** 停止并保留已识别的文字（stop 而非 abort，尽量把最后一句也带回来） */
        function stop() {
            wantListening = false;
            clearTimers();
            if (rec) {
                try { rec.stop(); } catch (e) { /* 已停止 */ }
            }
            state = 'idle';
            emitState('idle', 'user-stop');
        }

        function toggle() {
            return (state === 'listening' || state === 'starting') ? (stop(), false) : start();
        }

        function abort() {
            wantListening = false;
            clearTimers();
            if (rec) {
                try { rec.abort(); } catch (e) { /* 已停止 */ }
            }
            rec = null;
            state = 'idle';
            emitState('idle', 'abort');
        }

        return {
            toggle: toggle,
            start: start,
            stop: stop,
            abort: abort,
            get state() { return state; },
            get isListening() { return state === 'listening' || state === 'starting'; },
            /** 供按钮决定是否 disabled / 显示降级提示 */
            isSupported: function () { return unsupportedReason() === null; },
            unsupportedReason: unsupportedReason
        };
    }

    global.ZYVoice = {
        create: create,
        isSupported: function () { return unsupportedReason() === null; },
        unsupportedReason: unsupportedReason,
        explainError: explainError
    };
})(typeof window !== 'undefined' ? window : globalThis);