/**
 * 语音输入模块逻辑验证（assets/voice-input.js）
 *
 * 用 Node + 桩对象加载**真实模块源码**（不是复制一份逻辑来测），
 * 驱动 SpeechRecognition 的各类事件，验证：
 *   · 降级判定：浏览器不支持 / 局域网 http / localhost 三种情况
 *   · 错误映射：权限被拒、无麦克风、没说话、断网 → 都要给**可操作**提示
 *   · 识别流程：中间结果实时回填、且**不覆盖用户已输入的文字**
 *   · 续听：Chrome 静音自动结束后应自动重启，而不是直接停掉
 *   · 保护：超时自动停止、abort 能彻底收麦
 *
 * 运行：node scripts/verify_voice_input.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.join(__dirname, '..', 'frontend', 'assets', 'voice-input.js');
const code = fs.readFileSync(SRC, 'utf8');

let pass = 0, fail = 0;
function check(label, cond, detail) {
    if (cond) { pass++; console.log(`  ✓ ${label}` + (detail ? `  ← ${detail}` : '')); }
    else { fail++; console.log(`  ✗ ${label}` + (detail ? `  ← ${detail}` : '')); }
}

/** 构造一个可控的假 window */
function makeWindow(opts = {}) {
    const w = {
        location: opts.location || { protocol: 'http:', hostname: 'localhost' },
        isSecureContext: opts.isSecureContext !== undefined ? opts.isSecureContext : true,
    };
    if (opts.withRecognition) {
        w.SpeechRecognition = FakeSR;
    }
    w.__instances = [];
    return w;
}

/** 假的 SpeechRecognition，可被测试驱动 */
function FakeSR() {
    this.lang = ''; this.continuous = false; this.interimResults = false;
    this.started = 0; this.stopped = 0; this.aborted = 0;
    this.onresult = this.onerror = this.onend = this.onstart = null;
    // 注册到 window 便于断言
    if (typeof globalThis.__lastWin !== 'undefined') globalThis.__lastWin.__instances.push(this);
}
FakeSR.prototype.start = function () { this.started++; if (this.onstart) this.onstart(); };
FakeSR.prototype.stop = function () { this.stopped++; if (this.onend) this.onend(); };
FakeSR.prototype.abort = function () { this.aborted++; if (this.onend) this.onend(); };

/**
 * 伪造 onresult 事件，按 Web Speech API 的真实语义：
 *   · results 是**累积**的会话结果列表，本次变更从 resultIndex 开始
 *   · 一段话从 interim 转 final 是**更新同一索引**，不是新增一段
 *     （第二版桩错当成"新增"，导致同一句话被累加两遍 —— 是桩错，不是模块错）
 */
function makeEmitter(rec) {
    const acc = [];
    function emit(from) {
        const results = acc.map(c => [{ transcript: c.text, confidence: 0.9 }]);
        acc.forEach((c, i) => { results[i].isFinal = c.isFinal; });
        if (rec.onresult) rec.onresult({ results, resultIndex: from });
    }
    return {
        /** 新起一段（interim） */
        add(text) { acc.push({ text, isFinal: false }); emit(acc.length - 1); },
        /** 把第 idx 段标记为最终结果（浏览器就是这么回传的） */
        finalize(idx) {
            if (acc[idx]) acc[idx].isFinal = true;
            emit(idx);
        },
    };
}

function loadModule(win) {
    const ctx = vm.createContext({ window: win, setTimeout, clearTimeout, console });
    ctx.globalThis = ctx;
    vm.runInContext(code, ctx);
    return win.ZYVoice;
}

/** 造一个带录写的 voice 实例，返回状态收集器 */
function harness(win, opts = {}) {
    const rec = { text: opts.text || '' };
    const states = [];
    const notices = [];
    const V = loadModule(win);
    const voice = V.create({
        lang: 'zh-CN',
        maxSeconds: opts.maxSeconds || 60,
        getText: () => rec.text,
        setText: (t) => { rec.text = t; },
        onState: (s, d) => states.push(d ? s + ':' + d : s),
        onNotice: (m, l) => notices.push({ m, l }),
    });
    return { V, voice, rec, states, notices };
}

function lastInstance(win) { return win.__instances[win.__instances.length - 1]; }

// ────────────────────────── 1. 降级判定 ──────────────────────────
console.log('① 浏览器能力判定：不能静默失败，必须给出可操作原因');
{
    const V = loadModule(makeWindow({ withRecognition: false }));
    check('浏览器无 SpeechRecognition → 判定为不支持', V.isSupported() === false);
    check('原因是可操作的中文提示', /Chrome|Edge/.test(V.unsupportedReason()),
          V.unsupportedReason());
}
{
    const V = loadModule(makeWindow({
        withRecognition: true,
        location: { protocol: 'http:', hostname: '192.168.1.20' },
    }));
    check('局域网 IP + http → 判定为不支持（Chrome 会拦麦克风）', V.isSupported() === false);
    check('提示里点名 localhost 解决办法', /localhost/.test(V.unsupportedReason()),
          V.unsupportedReason());
}
{
    const V = loadModule(makeWindow({
        withRecognition: true,
        location: { protocol: 'http:', hostname: 'localhost' },
    }));
    check('localhost → 支持', V.isSupported() === true, V.unsupportedReason() || '可用');
}

// ────────────────────────── 2. 错误映射 ──────────────────────────
console.log('\n② 识别错误 → 面向用户的可操作提示');
{
    const V = loadModule(makeWindow({ withRecognition: true }));
    const cases = [
        ['not-allowed', /权限/],
        ['service-not-allowed', /权限/],
        ['audio-capture', /麦克风设备/],
        ['no-speech', /没有听到/],
        ['network', /联网/],
    ];
    cases.forEach(([code, re]) => {
        const msg = V.explainError(code);
        check(`${code} → 有可操作提示`, !!msg && re.test(msg), msg);
    });
    check('aborted（主动中止）不打扰用户', V.explainError('aborted') === '',
          JSON.stringify(V.explainError('aborted')));
}

// ────────────────────────── 3. 识别流程 ──────────────────────────
console.log('\n③ 识别结果回填：实时、不覆盖用户已输入的文字');
{
    const win = makeWindow({ withRecognition: true });
    globalThis.__lastWin = win;
    const { voice, rec, states } = harness(win, { text: '我想问' });

    voice.start();
    const r = lastInstance(win);
    check('start() 触发 onstart → 进入 listening', states.includes('listening'), states.join(' '));
    check('识别参数：中文 + 连续 + 中间结果',
          r.lang === 'zh-CN' && r.continuous === true && r.interimResults === true,
          `lang=${r.lang} continuous=${r.continuous} interim=${r.interimResults}`);

    // 真实事件序列：一段 interim → 转 final（同一索引）→ 新起一段 → 转 final
    const spk = makeEmitter(r);
    spk.add('红虫颗粒');
    check('中间结果实时回填且保留原有文字',
          rec.text === '我想问 红虫颗粒', JSON.stringify(rec.text));

    spk.finalize(0);                 // 同一段转 final —— 文字不应发生变化
    check('interim 转 final 时文字保持稳定（不重复累加、不掉空格）',
          rec.text === '我想问 红虫颗粒', JSON.stringify(rec.text));

    spk.add('还有货吗');
    check('连续两段中文直接相接（不塞多余空格）',
          rec.text === '我想问 红虫颗粒还有货吗', JSON.stringify(rec.text));

    spk.finalize(1);
    check('全部确认后文字依然稳定',
          rec.text === '我想问 红虫颗粒还有货吗', JSON.stringify(rec.text));

    // ⚠️ 关键约束：识别完**不自动发送**
    check('识别结束不会自动发送（发送权留给用户）',
          rec.text === '我想问 红虫颗粒还有货吗' && states.every(s => !s.startsWith('send')),
          rec.text);

    voice.stop();
    check('stop() 后回到 idle', voice.state === 'idle', voice.state);
}

// ────────────────────────── 4. 静音续听 ──────────────────────────
console.log('\n④ Chrome 静音自动结束后应续听，而不是让用户重按一次');
{
    const win = makeWindow({ withRecognition: true });
    globalThis.__lastWin = win;
    const { voice, states } = harness(win);
    voice.start();
    const r = lastInstance(win);
    const startedBefore = r.started;

    // 模拟浏览器静音 8 秒后自行结束（此时用户并未点停止）
    if (r.onend) r.onend();

    setTimeout(() => {
        check('onend 后自动重启识别', r.started > startedBefore,
              `start 调用 ${startedBefore} → ${r.started}`);
        check('续听期间用户意图仍是"正在听"', voice.isListening === true, voice.state);
        // 用户主动停止后不应再续
        voice.stop();
        const afterStop = r.started;
        if (r.onend) r.onend();
        setTimeout(() => {
            check('用户主动停止后不再续听', r.started === afterStop,
                  `stop 后 start 次数 ${afterStop} → ${r.started}`);

            // ───────────────── 5. 超时保护与彻底收麦 ─────────────────
            console.log('\n⑤ 超时自动停止 + abort 彻底收麦');
            const win2 = makeWindow({ withRecognition: true });
            globalThis.__lastWin = win2;
            const h2 = harness(win2, { maxSeconds: 0.05 });   // 50ms 超时，便于测试
            h2.voice.start();
            const r2 = lastInstance(win2);
            setTimeout(() => {
                check('达到单次时长上限后自动停止', h2.voice.state === 'idle', h2.voice.state);

                h2.voice.start();
                // ⚠️ 每次 start() 都会新建一个识别实例，断言必须取**最新**那个
                const r3 = lastInstance(win2);
                h2.voice.abort();
                check('abort() 会调用浏览器 abort（彻底关闭麦克风）', r3.aborted >= 1,
                      `abort 调用 ${r3.aborted} 次`);
                check('abort 后不再处于监听态', h2.voice.isListening === false, h2.voice.state);
                finish();
            }, 120);
        }, 500);
    }, 500);
}

function finish() {
    console.log('\n' + '='.repeat(62));
    console.log(`结果：${pass} 通过 / ${fail} 失败`);
    process.exit(fail ? 1 : 0);
}