package com.example;

import com.xiaomi.ai.api.Application;
import com.xiaomi.ai.api.Dialog;
import com.xiaomi.ai.api.General;
import com.xiaomi.ai.api.Nlp;
import com.xiaomi.ai.api.Settings;
import com.xiaomi.ai.api.Settings.ClientInfo;
import com.xiaomi.ai.api.SpeechRecognizer;
import com.xiaomi.ai.api.SpeechSynthesizer;
import com.xiaomi.ai.api.Template;
import com.xiaomi.ai.api.common.APIUtils;
import com.xiaomi.ai.api.common.Context;
import com.xiaomi.ai.api.common.Event;
import com.xiaomi.ai.auth.AuthProvider;
import com.xiaomi.ai.core.AivsConfig;
import com.xiaomi.ai.core.Channel;
import com.xiaomi.ai.core.ChannelListener;
import com.xiaomi.ai.core.InstructionWrapper;
import com.xiaomi.ai.core.WSChannel;
import com.xiaomi.ai.error.AivsError;
import com.xiaomi.ai.log.Logger;

import javax.sound.sampled.AudioFormat;
import javax.sound.sampled.AudioSystem;
import javax.sound.sampled.DataLine;
import javax.sound.sampled.TargetDataLine;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Map;

/**
 * AIVS 全双工语音对话 — 完整上下文版本
 * 对齐 bk7258 嵌入式项目: superXiaoai + RequestState + DialogState(userInSkillPage/skillId)
 */
public class AivsDemo {

    // --- 配置（可通过环境变量覆盖） ---
    private static String env(String key, String def) {
        String v = System.getenv(key);
        return (v != null && !v.isEmpty()) ? v : def;
    }

    private static final String CLIENT_ID  = env("AIVS_CLIENT_ID", "1392955592598882304");
    private static final String DEVICE_ID  = env("AIVS_DEVICE_ID", "998148728");
    private static final String MIOT_TOKEN = env("AIVS_MIOT_TOKEN", "tj0v60wfwq");
    private static final String MIOT_SESSION_ID = env("AIVS_MIOT_SID",
            "84146_" + DEVICE_ID + "_dummy_sid");
    private static final String USER_AGENT = "Robots.mk1; Build/0136 OS/BK7258 DID/" + DEVICE_ID;

    // 环境: preview / p4t / staging / production，默认 p4t
    private static final String AIVS_ENV = env("AIVS_ENV", "p4t");
    // 音频文件输入 or 文本输入（用于自动化测试）
    private static final String AUDIO_FILE = env("AIVS_INPUT_FILE", "");
    private static final String INPUT_TEXT = env("AIVS_INPUT_TEXT", "");
    // 是否是陌生人（对应 DialogState.isStranger），true/false，默认 false
    private static final boolean IS_STRANGER = "true".equalsIgnoreCase(env("AIVS_IS_STRANGER", "false"));
    // 角色 ID（对应 DialogState.roleId），可选，为空则不设置
    private static final String ROLE_ID = env("AIVS_ROLE_ID", "");
    private static final boolean isFile = !AUDIO_FILE.isEmpty();
    private static final boolean isText = !INPUT_TEXT.isEmpty();

    private static final int SAMPLE_RATE = 16000;
    private static final int BUFFER_BYTES = SAMPLE_RATE * 2 * 40 / 1000; // 40ms

    private static volatile boolean run = true;
    private static volatile boolean micMuted = false;
    private static Process ttsProcess = null;
    // 耗时统计
    private static long t0ConnectDone = 0;   // 连接完成
    private static long t1TextSent = 0;      // 文本发送完成
    private static long t2FirstReply = 0;    // 首个字回复
    private static volatile boolean t2Recorded = false;
    private static volatile String dialogId = "";
    private static volatile String replyErrorReason = "";
    private static volatile int replyErrorCode = 0;
    private static OutputStream ttsStdin = null;
    // 单槽传递：接收线程 put → 播放线程 take → write（不阻塞接收线程）
    private static byte[] ttsSlot = null;
    private static final Object ttsLock = new Object();
    // 文件模式下收集 SpeakStream 文本
    private static final StringBuilder ttsTextBuf = new StringBuilder();

    private static final SimpleDateFormat TS = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS");

    private static String ts() {
        synchronized (TS) { return TS.format(new Date()); }
    }

    public static void main(String[] args) throws Exception {
        // INFO 级别：显示初始化耗时打点（postData 已降为 DEBUG）
        Logger.setLogLevel(Logger.LOG_LEVEL_INFO);

        // -- 音频输入：文本模式跳过，文件模式读文件，否则麦克风 --
        final TargetDataLine mic;
        final InputStream fileInput;
        final boolean isFileLocal = !AUDIO_FILE.isEmpty();

        if (isText) {
            mic = null;
            fileInput = null;
        } else if (isFileLocal) {
            mic = null;
            InputStream tmp = new FileInputStream(AUDIO_FILE);
            // 跳过 WAV 头 (44 bytes)
            byte[] header = new byte[44];
            int read = tmp.read(header);
            if (read >= 4 && "RIFF".equals(new String(header, 0, 4))) {
                System.out.println(ts() + " [文件] WAV: " + AUDIO_FILE);
                fileInput = tmp;
            } else {
                tmp.close();
                fileInput = new FileInputStream(AUDIO_FILE);
                System.out.println(ts() + " [文件] RAW PCM: " + AUDIO_FILE);
            }
        } else {
            fileInput = null;
            AudioFormat fmt = new AudioFormat(SAMPLE_RATE, 16, 1, true, false);
            mic = (TargetDataLine) AudioSystem.getLine(
                    new DataLine.Info(TargetDataLine.class, fmt));
            mic.open(fmt, BUFFER_BYTES * 4);
            mic.start();
        }

        // -- SDK 配置（完整对齐 bk7258 embedded）--
        AivsConfig cfg = new AivsConfig();
        int envVal;
        switch (AIVS_ENV.toLowerCase()) {
            case "production": envVal = AivsConfig.ENV_PRODUCTION; break;
            case "staging":    envVal = AivsConfig.ENV_STAGING; break;
            case "preview":    envVal = AivsConfig.ENV_PREVIEW; break;
            case "p4t": default: envVal = AivsConfig.ENV_PREVIEW4TEST; break;
        }
        cfg.putInt(AivsConfig.ENV, envVal);
        cfg.putString(AivsConfig.Auth.CLIENT_ID, CLIENT_ID);
        cfg.putString(AivsConfig.Connection.USER_AGENT, USER_AGENT);
        cfg.putInt(AivsConfig.Connection.CONNECT_TIMEOUT, 2);
        cfg.putInt(AivsConfig.Connection.PING_INTERVAL, 10);
        cfg.putString(AivsConfig.Asr.CODEC, "PCM");
        cfg.putInt(AivsConfig.Asr.BITS, 16);
        cfg.putInt(AivsConfig.Asr.BITRATE, 16000);
        cfg.putInt(AivsConfig.Asr.CHANNEL, 1);
        cfg.putString(AivsConfig.Asr.LANG, "zh-CN");
        cfg.putBoolean(AivsConfig.Asr.ENABLE_PARTIAL_RESULT, true);
        cfg.putBoolean(AivsConfig.Asr.REMOVE_END_PUNCTUATION, true);
        cfg.putInt(AivsConfig.Asr.DUPLEX_TIMEOUT, 5);
        cfg.putInt(AivsConfig.Asr.OPUS_BITRATE, 32000);
        cfg.putInt(AivsConfig.Asr.VAD_TYPE, AivsConfig.Asr.VAD_TYPE_CLOUD);  // 云端 VAD 自行判停
        cfg.putString(AivsConfig.Tts.CODEC, AivsConfig.Tts.CODEC_MP3);
        cfg.putString(AivsConfig.Tts.AUDIO_TYPE, AivsConfig.Tts.AUDIO_TYPE_STREAM);
        cfg.putInt(AivsConfig.Offline.LOG_LEVEL, 1);
        cfg.putBoolean(AivsConfig.Track.ENABLE, false);

        ClientInfo ci = new ClientInfo();
        ci.setDeviceId(DEVICE_ID);
        ci.setCapabilitiesVersion(1);

        AuthProvider auth = new AuthProvider(Channel.AUTH_MIOT) {
            @Override public String getAuthHeader(boolean f, boolean t, Map<String, String> h) {
                String header = "MIOT-TOKEN-V1" + " app_id:" + CLIENT_ID
                        + ",session_id:" + MIOT_SESSION_ID
                        + ",token:" + MIOT_TOKEN
                        + ",device_id:" + DEVICE_ID;
                System.out.println("[鉴权] Authorization: " + header);
                return header;
            }
            @Override public String requestToken(boolean r, boolean t) { return MIOT_TOKEN; }
        };

        final WSChannel[] ch = new WSChannel[1];

        ChannelListener lis = new ChannelListener() {
            @Override public String onGetSSID() { return "unknown"; }
            @Override public void onInstruction(Channel c, InstructionWrapper iw) {
                String json = iw.getOriginal();
                String ns = extract(json, "namespace");
                String nm = extract(json, "name");

                // 记录 dialog_id（每个指令头都携带，取最后一个非空值）
                try {
                    if (iw.getInstruction() != null && iw.getInstruction().getDialogId() != null
                            && iw.getInstruction().getDialogId().isPresent()) {
                        String did = iw.getInstruction().getDialogId().get();
                        if (did != null && !did.isEmpty()) dialogId = did;
                    }
                } catch (Exception ignored) {}

                // 记录云端错误原因（Template.RepeatQuery 的 reason，如 timeout）
                if ("Template".equals(ns) && "RepeatQuery".equals(nm)) {
                    try {
                        Object payload = iw.getInstruction().getPayload();
                        if (payload instanceof Template.RepeatQuery) {
                            Template.RepeatQuery rq = (Template.RepeatQuery) payload;
                            if (rq.getReason() != null && rq.getReason().isPresent()) {
                                String reason = rq.getReason().get();
                                if (reason != null && !reason.isEmpty()) replyErrorReason = reason;
                            }
                        }
                    } catch (Exception ignored) {}
                }

                // 记录内容违规错误（Dialog.IllegalContent 的 code，如 411）
                if ("Dialog".equals(ns) && "IllegalContent".equals(nm)) {
                    try {
                        Object payload = iw.getInstruction().getPayload();
                        if (payload instanceof Dialog.IllegalContent) {
                            Dialog.IllegalContent ic = (Dialog.IllegalContent) payload;
                            if (ic.getCode() != null && ic.getCode().isPresent()) {
                                replyErrorCode = ic.getCode().get();
                            }
                        }
                    } catch (Exception ignored) {}
                }

                // TTS 播放 / 文件模式文本收集
                if ("SpeechSynthesizer".equals(ns) && "Speak".equals(nm)) {
                    if (isFile || isText) { ttsTextBuf.setLength(0); }
                    else {
                        micMuted = true;
                        synchronized (ttsLock) { ttsSlot = null; }
                        try {
                            final Process p = Runtime.getRuntime().exec(new String[]{
                                "ffplay", "-nodisp", "-autoexit",
                                "-fflags", "nobuffer", "-flags", "low_delay",
                                "-probesize", "32", "-analyzeduration", "0",
                                "-i", "pipe:0"});
                            ttsProcess = p;
                            ttsStdin = p.getOutputStream();
                            new Thread(new Runnable() { public void run() {
                                try {
                                    while (true) {
                                        byte[] chunk;
                                        synchronized (ttsLock) {
                                            while (ttsSlot == null) ttsLock.wait();
                                            chunk = ttsSlot; ttsSlot = null;
                                            ttsLock.notify();
                                        }
                                        if (chunk.length == 0) break;
                                        ttsStdin.write(chunk); ttsStdin.flush();
                                    }
                                    ttsStdin.close(); p.waitFor(); micMuted = false;
                                } catch (Exception e) {
                                    try { ttsStdin.close(); } catch (Exception ignored) {}
                                }
                            }}, "tts-player").start();
                            new Thread(new Runnable() { public void run() {
                                try { byte[] buf = new byte[256]; while (p.getErrorStream().read(buf) > 0); }
                                catch (Exception ignored) {}
                            }}).start();
                        } catch (Exception e) {}
                    }
                }
                if ("SpeechSynthesizer".equals(ns) && "SpeakStream".equals(nm)) {
                    // 用 payload 对象获取完整 text，避免手写 JSON 解析截断
                    String text = null;
                    try {
                        Object payload = iw.getInstruction().getPayload();
                        if (payload instanceof SpeechSynthesizer.SpeakStream) {
                            text = ((SpeechSynthesizer.SpeakStream) payload).getText();
                        }
                    } catch (Exception ignored) {}
                    if (text == null) text = extract(json, "text");
                    if (text != null && !text.isEmpty()) {
                        if (isFile || isText) ttsTextBuf.append(text);
                        if (!t2Recorded) {
                            t2Recorded = true;
                            t2FirstReply = System.currentTimeMillis();
                            if (t1TextSent > 0) {
                                System.out.println(ts() + " [耗时] 首个字回复 t2=" + t2FirstReply
                                        + ", 发送→首字=" + (t2FirstReply - t1TextSent) + "ms"
                                        + ", 初始化→首字=" + (t2FirstReply - t0ConnectDone) + "ms");
                            }
                        }
                    }
                }
                if ("SpeechSynthesizer".equals(ns) && "FinishSpeakStream".equals(nm)) {
                    if (!isFile) {
                        synchronized (ttsLock) {
                            while (ttsSlot != null) try { ttsLock.wait(); } catch (Exception e) {}
                            ttsSlot = new byte[0]; ttsLock.notify();
                        }
                    }
                }
                // 文件/文本模式：收到 Nlp.FinishStream 即打印结果（无需等 Dialog.Finish）
                if ("Nlp".equals(ns) && "FinishStream".equals(nm) && (isFile || isText)) {
                    if (isFile || isText) {
                        // 去掉换行符，确保 [回复] 完整单行输出（避免 ask.sh grep 截断）
                        String reply = ttsTextBuf.toString().trim().replace("\n", "").replace("\r", "");
                        if (!replyErrorReason.isEmpty()) {
                            reply = reply + " [reason=" + replyErrorReason + "]";
                        }
                        if (replyErrorCode != 0) {
                            reply = reply + " [code=" + replyErrorCode + "]";
                        }
                        System.out.println(ts() + " [回复] " + reply);
                        System.out.println(ts() + " [dialog_id] " + dialogId);
                    }
                    System.out.flush();
                    System.err.flush();
                    run = false;
                }

                System.out.println(ts() + " " + json);
            }
            @Override public void onBinaryMessage(Channel c, byte[] d) {
                // 文件模式不播放，麦克风模式通过单槽传递到 ffplay
                if (isFile) return;
                synchronized (ttsLock) {
                    while (ttsSlot != null) try { ttsLock.wait(); } catch (Exception e) {}
                    ttsSlot = d;
                    ttsLock.notify();
                }
            }
            @Override public boolean onWrite(Channel c, String k, String v) { return true; }
            @Override public String onRead(Channel c, String k) { return null; }
            @Override public void onRemove(Channel c, String k) {}
            @Override public void onClear(Channel c) {}
            @Override public void onError(Channel c, AivsError e) {
                System.err.println("[错误] " + e.getErrorCode() + ": " + e.getErrorMessage());
            }
            @Override public boolean isAllowCTA() { return true; }
        };

        System.out.println(ts() + " === 连接信息 ===");
        System.out.println("  env:           " + AIVS_ENV.toUpperCase());
        System.out.println("  clientId:      " + CLIENT_ID);
        System.out.println("  deviceId:      " + DEVICE_ID);
        System.out.println("  userAgent:     " + USER_AGENT);
        System.out.println("  cpv:           1");
        System.out.println("  pingInterval:  10s");
        System.out.println("  connectTimeout: 2s");
        System.out.println("  codec:         PCM 16kHz 16bit mono");
        System.out.println();

        // -- 连接 --
        WSChannel c = new WSChannel(cfg, ci, auth, lis);
        ch[0] = c;
        if (!c.start()) { if (mic != null) mic.close(); System.exit(1); }
        t0ConnectDone = System.currentTimeMillis();
        System.out.println(ts() + " [耗时] 连接完成 t0=" + t0ConnectDone);

        // -- 发送 DuplexRecognizeStarted（完整上下文）--
        SpeechRecognizer.DuplexRecognizeStarted pl = new SpeechRecognizer.DuplexRecognizeStarted();

        Settings.AudioFormat af = new Settings.AudioFormat();
        af.setCodec("PCM"); af.setBits(16); af.setRate(16000); af.setChannel(1);
        Settings.AsrConfig asr = new Settings.AsrConfig();
        asr.setFormat(af); pl.setAsr(asr);

        Settings.TtsConfig tts = new Settings.TtsConfig();
        tts.setCodec("MP3");
        tts.setAudioType(Settings.TtsAudioType.STREAM);
        tts.setStreamingAudioType(Settings.TtsAudioType.STREAM);
        tts.setVendor("AiNiRobot");
        pl.setTts(tts);

        List<Context> contexts = new ArrayList<Context>();

        // 1. superXiaoai (对齐 embedded)
        Application.ApplicationStatePayload sxaPl = new Application.ApplicationStatePayload();
        sxaPl.setSuperXiaoaiOn(true);
        Application.State sxa = new Application.State();
        sxa.setNextLevelState(sxaPl);
        contexts.add(APIUtils.buildContext(sxa));

        // 2. DialogState (continuous + userInSkillPage/id + isStranger)
        Dialog.DialogState ds = new Dialog.DialogState();
        ds.setContinuousDialog(true);
        ds.setUserInSkillPage(true);
        ds.setUserInSkillId("1120316263885969408");
        if (IS_STRANGER) {
            ds.setIsStranger(true);
        }
        if (!ROLE_ID.isEmpty()) {
            ds.setRoleId(ROLE_ID);
        }
        contexts.add(APIUtils.buildContext(ds));
        System.out.println(ts() + " [roleId] " + ROLE_ID);

        // 3. RequestState (isInitWakeup = true)
        General.RequestState rs = new General.RequestState();
        rs.setIsInitWakeup(true);
        contexts.add(APIUtils.buildContext(rs));

        Event<SpeechRecognizer.DuplexRecognizeStarted> ev = APIUtils.buildEvent(pl, contexts);
        c.postEvent(ev);

        System.out.println(ts() + " === 发送 DuplexRecognizeStarted ===");
        System.out.println(APIUtils.toJsonString(ev, true));
        System.out.println(ts() + " === contexts: " + contexts.size() + " ===");
        System.out.println();

        // --- 文本输入模式（直接发 Nlp.Request，不走 ASR）---
        if (isText) {
            Nlp.Request nlpReq = new Nlp.Request();
            nlpReq.setQuery(INPUT_TEXT);
            Event<Nlp.Request> nlpEv = APIUtils.buildEvent(nlpReq, contexts);
            c.postEvent(nlpEv);
            t1TextSent = System.currentTimeMillis();
            System.out.println(ts() + " === 发送 Nlp.Request: " + INPUT_TEXT + " ===");
            System.out.println(ts() + " [耗时] 文本已发送 t1=" + t1TextSent
                    + ", 初始化→发送=" + (t1TextSent - t0ConnectDone) + "ms");
            System.out.println();
        }

        // -- 音频采集（文本模式跳过）--
        Thread at = new Thread(new Runnable() { public void run() {
            if (isText) return;            byte[] b = new byte[BUFFER_BYTES]; long l = 0;
            try {
                while (run) {
                    int n;
                    if (isFile) {
                        // 文件模式：按 40ms 间隔模拟实时音频
                        n = fileInput.read(b, 0, b.length);
                        if (n <= 0) break;
                        Thread.sleep(Math.max(BUFFER_BYTES * 1000 / (SAMPLE_RATE * 2), 10));
                    } else {
                        if (!mic.isOpen()) break;
                        n = mic.read(b, 0, b.length);
                        if (n <= 0) continue;
                    }
                    long now = System.currentTimeMillis();
                    if (now - l > 1000) {
                        double s = 0; for (int i = 0; i < n; i += 2) s += ((b[i+1]<<8)|(b[i]&0xff)) * ((b[i+1]<<8)|(b[i]&0xff));
                        System.out.println(ts() + " [音量] " + (int)(20*Math.log10(Math.sqrt(s/(n/2))+1)) + "dB"
                                + (micMuted ? " [静音]" : ""));
                        l = now;
                    }
                    if (!micMuted && ch[0] != null && ch[0].isConnected()) {
                        byte[] d = new byte[n]; System.arraycopy(b,0,d,0,n); ch[0].postData(d);
                    }
                }
            } catch (Exception e) {}
            try { if (isFile) fileInput.close(); } catch (Exception e) {}
        }}, isFile ? "file" : "mic"); at.start();

        System.out.println("=== 连续对话模式（按 Enter 退出）===");
        System.out.println();

        // 无限等待，按 Enter 退出
        while (run) {
            if (System.in.available() > 0) { System.in.read(); break; }
            Thread.sleep(100);
        }

        run = false;
        Event<SpeechRecognizer.RecognizeStreamFinished> fe = APIUtils.buildEvent(new SpeechRecognizer.RecognizeStreamFinished());
        if (c.isConnected()) c.postEvent(fe);
        at.join(2000);
        if (!isFile && !isText) { mic.stop(); mic.close(); }
        c.stop();
    }

    private static String extractResultText(String j) {
        int pi = j.indexOf("\"payload\""); if (pi < 0) return null;
        int ri = j.indexOf("\"results\"", pi); if (ri < 0) return null;
        int ti = j.indexOf("\"text\"", ri); if (ti < 0) return null;
        int s = j.indexOf('"', ti+6); if (s < 0) return null;
        int e = j.indexOf('"', s+1); if (e < 0) return null;
        String t = j.substring(s+1, e); return t.isEmpty() ? null : t;
    }
    private static String extract(String j, String k) {
        String key = "\"" + k + "\""; int idx = j.indexOf(key); if (idx < 0) return null;
        int s = j.indexOf('"', idx+key.length()); if (s < 0) return null;
        int e = j.indexOf('"', s+1); if (e < 0) return null;
        return j.substring(s+1, e);
    }
    private static String trunc(String s, int m) { return s.length() <= m ? s : s.substring(0, m) + "..."; }

    // 解析 JSON 中某个字段的字符串值（处理嵌套转义 JSON）
    private static String parseTextValue(String json, String key) {
        String k = "\"" + key + "\"";
        int ki = json.indexOf(k); if (ki < 0) return null;
        // 跳过 :" 
        int vs = json.indexOf('"', ki + k.length() + 1);
        if (vs < 0) return null;
        // 从 payload 的 } 往前找结尾 " 
        int pi = json.indexOf("\"payload\"");
        int pe = json.lastIndexOf("}", pi >= 0 ? pi : json.length());
        if (pe < 0) pe = json.length() - 1;
        int ve = json.lastIndexOf('"', pe);
        if (ve <= vs) return null;
        return json.substring(vs + 1, ve);
    }
}
