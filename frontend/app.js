/**
 * 中渔天下企业官网 - 首页 Vue3 逻辑
 *
 * 功能：
 *   - SSE 流式 AI 对话 + 打字机效果 + 产品名片/引导卡
 *   - session_id 持久化（localStorage）
 *   - 历史消息恢复：进入页面时从后端 /api/agent/history/<sid>/ 拉取，跳转后不丢消息
 *   - 新对话：一键重置会话
 *   - 语音输入（assets/voice-input.js）+ 输入框随内容自增高
 */
const { createApp, ref, nextTick, onMounted } = Vue;

// ══════════════════════════════════════════════════════════════
// ZYAutoGrow —— 输入框随内容自增高（页面内聊天与悬浮弹窗共用）
// ══════════════════════════════════════════════════════════════
/**
 * textarea 的高度不能靠 CSS 自适应（内容不会撑开它），只能量 scrollHeight。
 * 关键是**先归零再量**：不归零的话，高度只会单调增长，永远缩不回去。
 *
 * 到上限时加 .is-overflowing → 转内部滚动（CSS 里未到上限时隐藏滚动条），
 * 这样长语音能完整看到、又不会把整个聊天区挤没。
 */
(function () {
    var MAX = 152;                 // 与 theme.css 的 max-height: 9.5rem 保持一致

    function sync(el) {
        if (!el) return 0;
        el.style.height = 'auto';
        var h = Math.min(el.scrollHeight || 0, MAX);
        el.style.height = h + 'px';
        // 归位：内容变短后移除溢出标记，滚动条随之隐藏
        if (el.scrollHeight > MAX + 1) {
            el.classList.add('is-overflowing');
        } else {
            el.classList.remove('is-overflowing');
        }
        return h;
    }

    /**
     * 绑定「随内容增高」。用 input 事件而非 keyup：
     * 语音识别是**直接改 value、不产生按键**，只监听 keyup 会漏掉语音输入。
     */
    function bind(el) {
        if (!el) return function () {};
        var onInput = function () { sync(el); };
        el.addEventListener('input', onInput);
        sync(el);
        return function unbind() { el.removeEventListener('input', onInput); };
    }

    window.ZYAutoGrow = { sync: sync, bind: bind, MAX: MAX };
})();

// ══════════════════════════════════════════════════════════════
// 顶层纯函数（不依赖组件状态）—— 便于 scripts/verify_order_card.js 直接断言
// ══════════════════════════════════════════════════════════════
/** 订单状态徽标配色：与个人中心同一套语义（进行中=湖青、待办=日落、终态=灰） */
function orderStatusClass(order) {
    const s = (order && order.status) || '';
    if (s === '已完成') return 'bg-brand-100 text-brand-700';
    if (s === '已发货') return 'bg-lake-50 text-lake-700';
    if (s === '已取消') return 'bg-stone-200 text-stone-600';
    return 'bg-sunset-50 text-sunset-700';            // 未发货 / 退货申请中：还有下一步要做
}

/** 可执行动作由后端下发（order_presenter.order_actions），前端只做展示判断，不重写规则 */
function hasAction(order, name) {
    return !!order && Array.isArray(order.actions) && order.actions.includes(name);
}

/** 金额统一两位小数（否则 5 显示成 5、12.5 显示成 12.5，卡片里不齐） */
function moneyOf(v) {
    return Number(v || 0).toFixed(2);
}

/**
 * 「猜你想问」：只在本轮真的弹了名片时给下一步 ——
 * 客户刚看完名片，最需要的正是「接下来能问什么」。
 */
function suggestFor(cards) {
    const list = Array.isArray(cards) ? cards : [];
    const orderCards = list.filter((c) => c && c.cardType === 'order');
    if (orderCards.length) {
        const unpaid = orderCards.some((c) => hasAction(c.order, 'pay'));
        return unpaid
            ? ['为什么要先付款？', '多久能发货？', '怎么申请退货？']
            : ['怎么查物流？', '怎么申请退货？', '还能再下单吗？'];
    }
    if (list.some((c) => c && c.cardType === 'product')) {
        return ['这款适合什么鱼？', '批发价是多少？', '还有别的推荐吗？'];
    }
    return [];
}

/** 相关度保留三位（tfidf 尺度能到 4+，语义只到 1，统一按数值截断不补零） */
function fmtScore(v) {
    const n = Number(v);
    if (!isFinite(n)) return '—';
    return n.toFixed(3);
}

/**
 * 把来源的「命中路径」翻译成一句人话。
 * 为什么值得显示：混合检索的说服力全在"这条路排第几"，
 * 只给一个融合分（0.03 量级）反而让人以为是 bug。
 */
function sourceRouteLabel(src) {
    if (!src || typeof src !== 'object') return '';
    const backend = src.backend || '';
    const lex = src.lexical_rank;
    const sem = src.semantic_rank;
    if (backend === 'hybrid') {
        const parts = [];
        if (lex) parts.push('字面路第 ' + lex);
        if (sem) parts.push('语义路第 ' + sem);
        const head = parts.length ? parts.join(' · ') : '单路召回';
        return head + ' → RRF 融合';
    }
    if (backend === 'embedding') {
        return (sem ? '语义路第 ' + sem : '语义路') + ' · 余弦相似度';
    }
    if (backend === 'tfidf') {
        return (lex ? '字面路第 ' + lex : '字面路') + ' · 余弦 + BM25';
    }
    return '';
}

createApp({
    setup() {
        // ========== session_id 持久化 ==========
        const SESSION_KEY = 'zyt_session_id';
        const getSessionId = () => {
            let sid = localStorage.getItem(SESSION_KEY);
            if (!sid) {
                sid = Math.random().toString(36).substring(2) + Date.now().toString(36);
                localStorage.setItem(SESSION_KEY, sid);
            }
            return sid;
        };

        // ========== 产品图本地映射（与 products.html 保持同一套） ==========
        // Products 表没有图片字段，产品图按名称映射到本地资源
        const PRODUCT_IMAGES = {
            '红虫颗粒': 'assets/images/product-hongchong.png',
            '九一八': 'assets/images/product-jiuyiba.png',
            '螺鲤3号': 'assets/images/product-luoli.png',
            '蓝鲫X5': 'assets/images/product-lanji.png',
            '速攻2号': 'assets/images/product-sugong.png',
        };
        const FALLBACK_IMG = 'assets/images/fallback.svg';

        // 产品名 → 本地产品图（未知产品回退通用饵料袋）
        const productImage = (name) => {
            if (!name) return FALLBACK_IMG;
            if (PRODUCT_IMAGES[name]) return PRODUCT_IMAGES[name];
            for (const key of Object.keys(PRODUCT_IMAGES)) {
                if (name.includes(key)) return PRODUCT_IMAGES[key];
            }
            return FALLBACK_IMG;
        };
        const onImgError = (e) => { e.target.src = FALLBACK_IMG; };

        // 不可购原因码 → 中文标签（与 products.html / order.html 同一套映射）
        const BLOCK_TAGS = {
            purchase_paused: '暂停接单',
            sold_out: '已售罄',
            no_inventory: '暂无库存',
            inactive: '已下架',
        };

        // 产品名片「立即下单」：把商品 id 带进下单页（order.html 会预选该商品）
        // —— 对应任务书四·3「点击卡片按钮直接进入下单界面」
        const goOrder = (product) => {
            if (!product || !product.id) {
                window.location.href = 'order.html';
                return;
            }
            window.location.href = 'order.html?product=' + encodeURIComponent(product.id);
        };

        // ========== 订单名片：对话内发起支付 ==========
        /**
         * 点「立即支付」→ 收银台 `/pay/<order_no>/`。
         *
         * 为什么不在对话里直接把订单标成已支付（早前是这么做的）：
         * 收银台现在承担「**支付方式选择**」的唯一职责 —— 扫码 / 余额两种走法、
         * 余额不足怎么引导充值，都在那一处实现。对话里再写一套就会重新长出
         * 「两个地方都能付、行为却不一致」的老毛病（和此前价格口径分裂同源）。
         * 名片该做的只是把用户**送到**付款这件事上，一屏之内完成。
         *
         * 保持 async：`payingOrders` / `payErrors` 两个响应式容器与模板绑定仍在，
         * 且验收脚本按 Promise 调用（`.catch`），不能改成同步返回值。
         */
        const payOrder = async (msg) => {
            const order = msg && msg.order;
            if (!order || !order.order_no) return;
            if (!window.AUTH || !(await AUTH.ensureLogin('登录后即可支付'))) return;
            window.location.href = '/pay/' + encodeURIComponent(order.order_no) + '/';
        };

        // ========== 追问建议（「猜你想问」） ==========
        // 只在本轮真的弹了卡片时给下一步（suggestFor 定义在文件头部，便于单测）
        const pushSuggest = (items) => {
            if (!items || !items.length) return;
            messages.value.push({ role: 'suggest', items: items.slice() });
            scrollToBottom();
        };

        // ========== 快捷提问 ==========
        const quickQuestions = ref([
            '钓鲤鱼用什么饵效果最好？',
            '红虫颗粒现在库存还有多少？',
            '批发10箱红虫颗粒怎么报价？',
            '我们支持7天无理由退换货吗？'
        ]);

        // ========== 欢迎消息（历史为空时显示） ==========
        const welcomeMessage = {
            role: 'ai',
            content: '您好，我是「中渔小助」\n中渔天下的专属饵料顾问，全天在线。\n\n我可以帮您：\n· 查产品价格与规格\n· 查库存与到货\n· 算批发报价、下订单\n· 问售后政策\n· 聊钓鱼技巧与季节钓法\n\n试试点击下面的快捷问题，马上开聊～'
        };

        // ========== 未登录提示（AI 对话已纳入登录保护） ==========
        const loginPromptMessage = {
            role: 'ai',
            content: '您好，我是「中渔小助」。\n\n为了记录您的咨询与订单信息，请先登录后再开始对话。\n登录后我可以帮您：\n· 查产品价格与规格\n· 查库存与到货\n· 算批发报价、下订单\n· 问售后政策\n\n点击右上角「登录」即可开始，登录成功后回到本页继续咨询。'
        };

        // 当前是否已登录（对话与历史均需登录态）
        const loginRequired = () => !(window.AUTH && AUTH.isLoggedIn());

        // ========== 聊天状态 ==========
        const messages = ref([welcomeMessage]);
        const inputText = ref('');
        const loading = ref(false);
        const historyLoaded = ref(false);
        const messageBox = ref(null);
        const pendingGuide = ref(false);
        // 本轮 Agent 介绍过的商品（产品名片，可一键下单）
        const pendingProducts = ref([]);
        // 本轮 Agent 查过的订单（订单名片，可直接支付 / 看物流）
        const pendingOrders = ref([]);
        // 对话内支付：按订单号记录「支付中」与错误信息（卡片按钮据此禁用与提示）
        const payingOrders = ref({});
        const payErrors = ref({});

        // ========== 语音输入 ==========
        // 用浏览器原生 Web Speech API（见 assets/voice-input.js）。
        // 关键约束：**识别结果只回填输入框，不自动发送** —— 这个 Agent 能真实
        // 下单 / 查单 / 发起退货，语音把商品名听错就自动发出去等于让用户为一次
        // 误识别背单。所以发送与否永远由用户自己点。
        const voiceSupported = ref(typeof window.ZYVoice !== 'undefined' && ZYVoice.isSupported());
        const voiceUnsupportedReason = ref(
            (typeof window.ZYVoice !== 'undefined' && ZYVoice.unsupportedReason()) || '语音输入不可用'
        );
        const voiceListening = ref(false);
        const voiceNotice = ref('');
        const voiceNoticeLevel = ref('info');
        const chatInputEl = ref(null);          // 模板里的 ref，供语音回填
        let voice = null;                        // 非响应式实例，勿放进 ref

        const toggleVoice = () => {
            if (!voice) return;
            voice.toggle();
        };

        // 模板与生命周期之间用 DOM 兜底拿到输入框（ref 在 onMounted 后才可靠）
        const voiceGetText = () =>
            (chatInputEl.value && chatInputEl.value.value) || inputText.value || '';
        const voiceSetText = (t) => {
            inputText.value = t;                 // v-model 同步更新
            if (chatInputEl.value) {
                chatInputEl.value.value = t;
                // 语音是逐段追加的（没有按键事件），必须手动重算高度
                if (window.ZYAutoGrow) window.ZYAutoGrow.sync(chatInputEl.value);
            }
        };

        const initVoice = () => {
            if (typeof window.ZYVoice === 'undefined') {
                voiceSupported.value = false;
                voiceUnsupportedReason.value = '语音模块未加载，请刷新页面重试。';
                return;
            }
            voiceSupported.value = ZYVoice.isSupported();
            voiceUnsupportedReason.value = ZYVoice.unsupportedReason() || '';
            if (!voiceSupported.value) return;

            voice = ZYVoice.create({
                lang: 'zh-CN',
                getText: voiceGetText,
                setText: voiceSetText,
                onState: (state) => {
                    const listening = state === 'listening' || state === 'starting';
                    voiceListening.value = listening;
                    if (listening) {
                        voiceNotice.value = '正在聆听…说完点一下方块停止，确认文字无误后点发送';
                        voiceNoticeLevel.value = 'info';
                    } else if (state === 'idle') {
                        voiceNotice.value = '';
                    }
                },
                onNotice: (msg, level) => {
                    voiceNotice.value = msg;
                    voiceNoticeLevel.value = level === 'error' ? 'error' : 'warn';
                },
            });
        };
        // 进入页面即初始化：麦克风权限在用户点击时才会弹，初始化不触发授权
        initVoice();

        // ========== 输入框自增高 ==========
        const syncGrow = () => {
            if (chatInputEl.value) window.ZYAutoGrow.sync(chatInputEl.value);
        };
        // 语音回填后立刻重算高度：识别是一段段追加的，不重算会停留在单行高度
        /**
         * Enter 发送 / Shift+Enter 换行。
         *
         * ⚠️ **必须判断输入法组字状态**：中文输入法打拼音时按 Enter 是"选词"，
         * 若直接发消息，用户刚打完"红虫颗粒"就被弹出去了，一个字都发不出去。
         * 浏览器在组字期间会给出 `isComposing === true`（老浏览器是 keyCode 229）。
         */
        const onInputKeydown = (e) => {
            if (e.key !== 'Enter') return;
            if (e.isComposing || e.keyCode === 229) return;    // 组字中：不发送
            if (e.shiftKey) return;                             // Shift+Enter：换行
            e.preventDefault();
            sendMessage();
        };

        // 滚动到底部
        const scrollToBottom = () => {
            nextTick(() => {
                if (messageBox.value) {
                    messageBox.value.scrollTop = messageBox.value.scrollHeight;
                }
            });
        };

        /**
         * 从后端恢复历史消息（跳转/刷新后不丢消息）
         * 未登录时不请求接口，直接展示登录引导（历史接口已纳入登录保护）
         */
        const loadHistory = async () => {
            if (loginRequired()) {
                messages.value = [loginPromptMessage];
                historyLoaded.value = true;
                scrollToBottom();
                return;
            }
            const sid = getSessionId();
            try {
                const resp = await AUTH.authFetch(`/api/agent/history/${sid}/`, {}, { silentAuthFail: true });
                if (resp.status === 401) {
                    messages.value = [loginPromptMessage];
                    historyLoaded.value = true;
                    scrollToBottom();
                    return;
                }
                const data = await resp.json();
                if (data.success && Array.isArray(data.messages) && data.messages.length > 0) {
                    messages.value = data.messages.map(m => ({
                        role: m.role === 'assistant' ? 'ai' : 'user',
                        content: (m.content || '').replace(/\*\*/g, ''),
                    }));
                } else {
                    messages.value = [welcomeMessage];
                }
            } catch (e) {
                messages.value = [welcomeMessage];
            }
            historyLoaded.value = true;
            scrollToBottom();
        };

        /**
         * 新对话：重置会话 ID 并清空当前消息
         */
        const newChat = () => {
            if (loading.value) return;
            const sid = Math.random().toString(36).substring(2) + Date.now().toString(36);
            localStorage.setItem(SESSION_KEY, sid);
            messages.value = [loginRequired() ? loginPromptMessage : welcomeMessage];
            pendingProducts.value = [];
            pendingOrders.value = [];
            scrollToBottom();
        };

        /**
         * 下单成功后跳回本页时带 `?order=<订单号>`（order.html 成功页的「回到咨询」）。
         *
         * 这里拉一次订单详情、在对话里补一张订单名片 —— 接上「下单 → 回到对话 → 支付/查单」
         * 这最后一跳。改造前客户下完单只能留在下单页，回到对话里 Agent 对这笔单毫无所知。
         */
        const showOrderCardFromUrl = async () => {
            const orderNo = new URLSearchParams(location.search).get('order');
            if (!orderNo) return;
            // 立刻清掉参数：刷新/再次进入不重复弹卡（卡片已经在会话里了）
            history.replaceState(null, '', location.pathname + location.hash);
            if (loginRequired()) return;
            try {
                const resp = await AUTH.authFetch(
                    '/api/orders/' + encodeURIComponent(orderNo) + '/',
                    {}, { silentAuthFail: true }
                );
                if (!resp.ok) return;
                const data = await resp.json();
                const order = data && (data.order || data);
                if (!order || !order.order_no) return;
                messages.value.push({
                    role: 'ai',
                    content: '这笔订单已经记录好了，后面进度我随时能帮你查。',
                });
                messages.value.push({ role: 'card', cardType: 'order', order });
                pushSuggest(suggestFor([{ cardType: 'order', order }]));
                scrollToBottom();
            } catch (e) {
                // 拉不到订单详情不打断聊天页，静默降级
                console.warn('回显订单名片失败：', e);
            }
        };

        // 进入页面即加载历史；若带 ?order= 再补一张订单名片（下单成功回到对话的闭环）
        onMounted(async () => {
            await loadHistory();
            await showOrderCardFromUrl();
            // 模板 ref 此时才可用：绑定自增高（语音回填另有单独的重算）
            await nextTick();
            if (chatInputEl.value && window.ZYAutoGrow) {
                window.ZYAutoGrow.bind(chatInputEl.value);
            }
        });

        /**
         * 解析 SSE 行：只处理 data: 开头的行
         */
        const parseSSELine = (line) => {
            const trimmed = line.trim();
            if (!trimmed.startsWith('data:')) return null;
            const jsonStr = trimmed.substring(5).trim();
            try {
                return JSON.parse(jsonStr);
            } catch (e) {
                return null;
            }
        };

        /**
         * 强制触发 Vue 响应式更新
         */
        const forceReactivityUpdate = (idx) => {
            if (idx < messages.value.length) {
                const item = messages.value[idx];
                messages.value.splice(idx, 1, item);
            }
        };

        /**
         * 发送消息（SSE 流式版本）
         */
        const sendMessage = async () => {
            const text = inputText.value.trim();
            if (!text || loading.value) return;

            // 登录守卫：未登录 / 登录态失效 → 跳登录页并带回跳参数
            if (!window.AUTH || !(await AUTH.ensureLogin('登录后即可与中渔小助对话'))) return;

            // 已发送 → 收麦，避免麦克风一直开着录到 AI 的回答
            if (voice && voice.isListening) voice.stop();

            inputText.value = '';
            // 发送后归位到单行（下一条从干净状态开始长）
            if (chatInputEl.value && window.ZYAutoGrow) window.ZYAutoGrow.sync(chatInputEl.value);

            messages.value.push({ role: 'user', content: text });
            loading.value = true;
            scrollToBottom();

            const aiMsgIdx = messages.value.length;
            messages.value.push({ role: 'ai', content: '', streaming: true });
            scrollToBottom();

            let streamEnded = false;
            pendingGuide.value = false;
            pendingProducts.value = [];
            pendingOrders.value = [];

            try {
                const sessionId = getSessionId();

                const response = await AUTH.authFetch('/api/agent/chat/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        message: text,
                        session_id: sessionId,
                    }),
                });

                if (response.status === 401) {
                    messages.value.splice(aiMsgIdx, 1, {
                        role: 'error',
                        content: '登录状态已失效，请重新登录后再试。'
                    });
                    loading.value = false;
                    return;
                }

                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';

                while (!streamEnded) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });

                    const parts = buffer.split('\n\n');
                    buffer = parts.pop();

                    for (const part of parts) {
                        if (streamEnded) break;
                        const lines = part.split('\n');
                        for (const line of lines) {
                            if (streamEnded) break;
                            const data = parseSSELine(line);
                            if (!data) continue;

                            // session_id 更新
                            if (data.session_id) {
                                localStorage.setItem(SESSION_KEY, data.session_id);
                            }

                            // 引导转化卡片（去定制服务 / 联系我们）
                            if (data.type === 'guide_card') {
                                pendingGuide.value = true;
                                continue;
                            }

                            // 产品名片（名称/规格/图片/价格/库存 + 一键下单）
                            if (data.type === 'product_card' && data.product) {
                                const p = data.product;
                                pendingProducts.value.push({
                                    ...p,
                                    stock_label: p.can_buy
                                        ? ('现货 ' + (p.available_stock || 0) + ' 包')
                                        : (BLOCK_TAGS[p.buy_block_reason] || '暂不可购'),
                                });
                                continue;
                            }

                            // 订单名片（订单号/商品/金额/状态/物流 + 立即支付 / 我的订单）
                            if (data.type === 'order_card' && data.order) {
                                pendingOrders.value.push(data.order);
                                continue;
                            }

                            // 引用来源（这条回答依据了知识库哪几条、相关度多少）
                            // 单独事件而不是塞进正文：来源不是回答内容，
                            // 混进 content 会被落库、也会被下一轮当成上下文喂回模型。
                            if (data.type === 'sources' && Array.isArray(data.sources)) {
                                if (messages.value[aiMsgIdx]) {
                                    messages.value[aiMsgIdx].sources = data.sources;
                                    messages.value[aiMsgIdx].sourcesOpen = false;
                                    forceReactivityUpdate(aiMsgIdx);
                                }
                                continue;
                            }

                            // 流结束信号
                            if (data.done) {
                                if (messages.value[aiMsgIdx]) {
                                    messages.value[aiMsgIdx].streaming = false;
                                    if (!messages.value[aiMsgIdx].content) {
                                        messages.value[aiMsgIdx].content = '（未收到回复，请重试）';
                                    }
                                    forceReactivityUpdate(aiMsgIdx);
                                }
                                streamEnded = true;
                                break;
                            }

                            // 文本内容追加
                            if (data.content && messages.value[aiMsgIdx]) {
                                messages.value[aiMsgIdx].content += data.content;
                                forceReactivityUpdate(aiMsgIdx);
                                scrollToBottom();
                            }
                        }
                    }
                }

                // 流结束后统一插卡，固定顺序：产品名片 → 订单名片 → 引导卡
                // （拆成多次 splice 会因 index 相同而倒序，所以先收集再一次性插入）
                const trailingCards = [];
                pendingProducts.value.forEach((p) => {
                    trailingCards.push({ role: 'card', cardType: 'product', product: p });
                });
                // 订单名片（查订单进度 → 结构化卡片，带「立即支付」）
                pendingOrders.value.forEach((o) => {
                    trailingCards.push({ role: 'card', cardType: 'order', order: o });
                });
                // 引导转化卡片（去定制服务 / 联系我们）
                // 优先级1：后端发了 guide_card 事件
                // 优先级2：AI 回复文本含引导词（后端未走工具时的兜底）
                let needGuide = pendingGuide.value;
                if (!needGuide) {
                    const aiText = messages.value[aiMsgIdx] ? (messages.value[aiMsgIdx].content || '') : '';
                    needGuide = /(定制服务|联系我们|提交需求|联系客服)/.test(aiText);
                }
                if (needGuide) {
                    trailingCards.push({ role: 'card', cardType: 'guide' });
                }
                if (trailingCards.length) {
                    nextTick(() => {
                        trailingCards.forEach((c, i) => {
                            messages.value.splice(aiMsgIdx + 1 + i, 0, c);
                        });
                        scrollToBottom();
                    });
                    // 名片之后给「猜你想问」：把下一步摆到客户手边（点一下即发送）
                    pushSuggest(suggestFor(trailingCards));
                }
            } catch (err) {
                console.error('SSE 接收失败：', err);
                if (!streamEnded) {
                    messages.value.splice(aiMsgIdx, 1, {
                        role: 'error',
                        content: '连接后端失败，请检查后端服务是否启动。'
                    });
                }
            }

            loading.value = false;
            nextTick(() => {
                const input = document.querySelector('.chat-input');
                if (input) input.focus();
            });
            scrollToBottom();
        };

        const sendQuickQuestion = (question) => {
            if (loading.value) return;
            inputText.value = question;
            sendMessage();
        };

        const goToPage = (url) => {
            window.location.href = url;
        };

        /**
         * 展开 / 收起某条回答的「参考来源」。
         * 状态挂在消息对象上（sourcesOpen），而不是组件级：同一屏里多条回答
         * 可以各开各的，互不影响。
         */
        const toggleSources = (idx) => {
            const msg = messages.value[idx];
            if (!msg || !msg.sources) return;
            msg.sourcesOpen = !msg.sourcesOpen;
            forceReactivityUpdate(idx);
        };

        return {
            quickQuestions,
            messages,
            inputText,
            loading,
            historyLoaded,
            messageBox,
            sendMessage,
            sendQuickQuestion,
            goToPage,
            newChat,
            productImage,
            onImgError,
            goOrder,
            toggleSources,
            // 引用来源（纯函数，便于 verify_citations.js 直接断言）
            fmtScore,
            sourceRouteLabel,
            // 订单名片（纯逻辑取自文件头部的顶层函数，便于单测；模板里按 money(...) 调用）
            orderStatusClass,
            hasAction,
            money: moneyOf,
            payOrder,
            payingOrders,
            payErrors,
            // ── 语音输入 ──
            voiceSupported,
            voiceUnsupportedReason,
            voiceListening,
            voiceNotice,
            voiceNoticeLevel,
            chatInputEl,
            toggleVoice,
            syncGrow,
            onInputKeydown,
        };
    },
}).mount('#app');
