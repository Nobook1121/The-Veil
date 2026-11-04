/**
 * 合成声部库：每个函数对应一种「电影配乐声部」，而不是通用的和弦垫。
 *
 * 声部写作上的硬规则：
 * - 弦乐用锯齿波 + 低通 + 缓慢起音 + 轻微颤音，长音之间允许重叠，制造「弓弦持续」感；
 * - 铜管用带通扫频模拟 formant，起音更晚、渐强更陡，只在高潮段落进入；
 * - 管风琴/持续音用整数谐波叠加，稳定、无颤音，负责 pedal point；
 * - 定音鼓 = 低频正弦的下滑音高 + 低通噪声，承担「心跳」节奏；
 * - 钟声用非整数倍频程（1 : 2.76），衰减长，用于 cue 的钩子与留白后的第一声；
 * - 所有声部都输出到总线的低通/动态链，混响与延迟由总线统一发送，避免每个声部各自加湿。
 *
 * 低性能设备（quality=2）只保留最必要的正弦/三角波骨架，不创建颤音 LFO、钟声与噪声。
 */
namespace TrpgImmersiveMusicVoices {
    export interface VoiceHost {
        ctx: AudioContext;
        noise: AudioBuffer;
        out: AudioNode;
        /** 0 = 完整，1 = 精简，2 = 最小。 */
        quality: number;
        random: () => number;
    }

    const NOTE_A4 = 69;
    const FREQ_A4 = 440;

    export function midiToFreq(note: number): number {
        const safe = Number.isFinite(note) ? note : 60;
        return FREQ_A4 * Math.pow(2, (safe - NOTE_A4) / 12);
    }

    function clampValue(value: number, min: number, max: number): number {
        if (!Number.isFinite(value)) return min;
        return Math.max(min, Math.min(max, value));
    }

    export class Rack {
        private readonly host: VoiceHost;

        constructor(host: VoiceHost) {
            this.host = host;
        }

        private get ctx(): AudioContext {
            return this.host.ctx;
        }

        private get quality(): number {
            return this.host.quality;
        }

        private envelope(start: number, duration: number, gain: number, attack: number, release: number): GainNode {
            const node = this.ctx.createGain();
            const safeDuration = Math.max(0.08, duration);
            const safeAttack = Math.min(Math.max(0.02, attack), safeDuration * 0.5);
            const safeRelease = Math.min(Math.max(0.05, release), safeDuration * 0.55);
            const peakTime = start + safeAttack;
            const releaseTime = Math.max(peakTime, start + safeDuration - safeRelease);
            node.gain.setValueAtTime(0.0001, start);
            node.gain.linearRampToValueAtTime(Math.max(0.0002, gain), peakTime);
            node.gain.setValueAtTime(Math.max(0.0002, gain), releaseTime);
            node.gain.exponentialRampToValueAtTime(0.0001, start + safeDuration);
            node.connect(this.host.out);
            return node;
        }

        private osc(type: OscillatorType, note: number, start: number, duration: number, detune = 0): OscillatorNode {
            const node = this.ctx.createOscillator();
            node.type = type;
            node.frequency.setValueAtTime(midiToFreq(note), start);
            node.detune.setValueAtTime(detune, start);
            node.start(start);
            node.stop(start + Math.max(0.08, duration));
            return node;
        }

        /** 人化：起音与音分微差，避免所有声部相位锁死成「合成器味」。 */
        private jitter(amount: number): number {
            return (this.host.random() - 0.5) * amount * 2;
        }

        private detune(base: number, spread = 7): number {
            return base + this.jitter(spread);
        }

        private startAt(start: number, amount = 0.012): number {
            return Math.max(0, start + this.jitter(amount));
        }

        /**
         * 瞬态/气息层：用滤波噪声给合成音色加「真实乐器」的起音与空气感。
         * 只用于完整/精简档；最小档（quality=2）不创建，保持移动端开销可控。
         */
        private air(
            target: AudioNode,
            start: number,
            duration: number,
            gain: number,
            type: BiquadFilterType,
            frequency: number,
            q = 0.8,
        ): void {
            if (this.quality >= 2 || !this.host.noise) return;
            const source = this.ctx.createBufferSource();
            source.buffer = this.host.noise;
            source.loop = true;
            const offset = this.host.random() * Math.max(0.01, this.host.noise.duration - 1);
            const filter = this.ctx.createBiquadFilter();
            filter.type = type;
            filter.frequency.setValueAtTime(clampValue(frequency, 80, 9000), start);
            filter.Q.setValueAtTime(q, start);
            const level = this.ctx.createGain();
            const safe = Math.max(0.03, duration);
            level.gain.setValueAtTime(0.0001, start);
            level.gain.linearRampToValueAtTime(Math.max(0.0003, gain), start + Math.min(0.08, safe * 0.4));
            level.gain.exponentialRampToValueAtTime(0.0001, start + safe);
            source.connect(filter);
            filter.connect(level);
            level.connect(target);
            source.start(start, offset);
            source.stop(start + safe + 0.05);
        }

        /** 长音和声垫：每个音 1~2 个振荡器，整体一个低通，避免逐音建滤波器。 */
        pad(notes: number[], start: number, duration: number, gain: number, cutoff: number, pan = 0): void {
            const list = notes.filter((note) => Number.isFinite(note));
            if (!list.length) return;
            const at = this.startAt(start, 0.01);
            const filter = this.ctx.createBiquadFilter();
            filter.type = "lowpass";
            filter.frequency.setValueAtTime(clampValue(cutoff, 120, 6000), at);
            filter.Q.setValueAtTime(0.6, at);

            const panner = this.ctx.createStereoPanner();
            panner.pan.setValueAtTime(clampValue(pan, -0.8, 0.8), at);
            filter.connect(panner);
            panner.connect(this.host.out);

            const envelope = this.envelope(at, duration, gain, 0.5, 0.55);
            envelope.connect(filter);

            const perNote = gain / Math.max(1, list.length);
            const oscCount = this.quality >= 2 ? 1 : 2;
            list.forEach((note) => {
                for (let index = 0; index < oscCount; index += 1) {
                    const osc = this.osc(
                        index === 0 ? "triangle" : "sawtooth",
                        note,
                        at,
                        duration,
                        this.detune((index === 0 ? -1 : 1) * (this.quality >= 2 ? 2 : 7), 4),
                    );
                    const mix = this.ctx.createGain();
                    mix.gain.value = perNote / oscCount;
                    osc.connect(mix);
                    mix.connect(filter);
                }
            });
        }

        /** 弦乐：锯齿波 + 低通 + 轻微颤音，起音比 pad 慢。 */
        strings(notes: number[], start: number, duration: number, gain: number, cutoff: number, pan = 0): void {
            const list = notes.filter((note) => Number.isFinite(note));
            if (!list.length) return;
            const at = this.startAt(start, 0.015);
            const filter = this.ctx.createBiquadFilter();
            filter.type = "lowpass";
            filter.frequency.setValueAtTime(clampValue(cutoff * 1.6, 180, 7000), at);
            filter.Q.setValueAtTime(0.9, at);
            const panner = this.ctx.createStereoPanner();
            panner.pan.setValueAtTime(clampValue(pan, -0.85, 0.85), at);
            filter.connect(panner);
            panner.connect(this.host.out);
            // 弓噪：真实弦乐起音时的高频摩擦，是「合成弦乐」最缺的一层。
            this.air(panner, at, Math.min(0.7, duration * 0.55), gain * 0.12, "bandpass", 2700, 1.2);
            const envelope = this.envelope(at, duration, gain, 0.7, 0.5);
            envelope.connect(filter);

            const perNote = gain / Math.max(1, list.length);
            list.forEach((note, noteIndex) => {
                const osc = this.osc(
                    "sawtooth",
                    note,
                    at,
                    duration,
                    this.detune(noteIndex % 2 === 0 ? -6 : 6, 5),
                );
                const tone = this.ctx.createGain();
                tone.gain.value = perNote * 0.85;
                osc.connect(tone);
                tone.connect(filter);
                if (this.quality <= 0) {
                    const vibrato = this.ctx.createOscillator();
                    vibrato.type = "sine";
                    vibrato.frequency.value = 4.6 + noteIndex * 0.35;
                    const depth = this.ctx.createGain();
                    depth.gain.value = 7;
                    vibrato.connect(depth);
                    depth.connect(osc.detune);
                    vibrato.start(at);
                    vibrato.stop(at + Math.max(0.1, duration));
                }
            });
        }

        /** 铜管：带通 formant 扫频 + 迟起音 + 上滑音，只在高潮进入。 */
        brass(notes: number[], start: number, duration: number, gain: number, cutoff: number): void {
            const list = notes.filter((note) => Number.isFinite(note));
            if (!list.length) return;
            const at = this.startAt(start, 0.02);
            const formant = this.ctx.createBiquadFilter();
            formant.type = "bandpass";
            formant.frequency.setValueAtTime(420, at);
            formant.frequency.linearRampToValueAtTime(900, at + Math.min(0.6, duration * 0.4));
            formant.Q.setValueAtTime(1.4, at);
            const air = this.ctx.createBiquadFilter();
            air.type = "lowpass";
            air.frequency.setValueAtTime(clampValue(cutoff * 2.2, 300, 9000), at);
            formant.connect(air);
            air.connect(this.host.out);
            // 铜管气息：起音瞬间的气流噪声，让 formant 扫频不至于像纯锯齿波。
            this.air(air, at, Math.min(0.45, duration * 0.4), gain * 0.16, "bandpass", 760, 0.9);

            const envelope = this.envelope(at, duration, gain, 0.32, 0.45);
            envelope.connect(formant);

            const perNote = gain / Math.max(1, list.length);
            list.forEach((note, index) => {
                const osc = this.osc(
                    "sawtooth",
                    note,
                    at,
                    duration,
                    this.detune(index % 2 === 0 ? -4 : 4, 5),
                );
                osc.frequency.setValueAtTime(midiToFreq(note - 2), at);
                osc.frequency.linearRampToValueAtTime(midiToFreq(note), at + Math.min(0.35, duration * 0.3));
                const tone = this.ctx.createGain();
                tone.gain.value = perNote * 0.8;
                osc.connect(tone);
                tone.connect(formant);
            });
        }

        /** 管风琴式持续音：整数谐波叠加，稳定无颤音，承担 pedal point。 */
        organ(notes: number[], start: number, duration: number, gain: number): void {
            const list = notes.filter((note) => Number.isFinite(note));
            if (!list.length) return;
            const out = this.ctx.createGain();
            out.gain.value = gain / Math.max(1, list.length);
            out.connect(this.host.out);
            const envelope = this.envelope(start, duration, 1, 0.3, 0.45);
            envelope.connect(out);
            const harmonics = this.quality >= 2 ? [1, 2] : [1, 2, 3, 4];
            list.forEach((note) => {
                harmonics.forEach((harmonic, index) => {
                    const osc = this.osc("sine", note + 12 * Math.log2(harmonic), start, duration, 0);
                    const level = this.ctx.createGain();
                    level.gain.value = 0.55 / (index + 1);
                    osc.connect(level);
                    level.connect(envelope);
                });
            });
        }

        /** 人声般的合唱垫：带通共振 + 缓慢颤音。 */
        choir(notes: number[], start: number, duration: number, gain: number): void {
            if (this.quality >= 2) return;
            const list = notes.filter((note) => Number.isFinite(note));
            if (!list.length) return;
            const at = this.startAt(start, 0.02);
            const formant = this.ctx.createBiquadFilter();
            formant.type = "bandpass";
            formant.frequency.setValueAtTime(760, at);
            formant.Q.setValueAtTime(1.1, at);
            formant.connect(this.host.out);
            // 合唱呼吸：长音下的低幅气流噪声，避免「一群人形正弦波」。
            this.air(formant, at, Math.min(1.2, duration * 0.7), gain * 0.1, "bandpass", 640, 0.9);
            const envelope = this.envelope(at, duration, gain, 0.8, 0.7);
            envelope.connect(formant);
            const perNote = gain / Math.max(1, list.length);
            list.forEach((note, index) => {
                const osc = this.osc(
                    index % 2 === 0 ? "sine" : "triangle",
                    note,
                    at,
                    duration,
                    this.detune(index % 2 === 0 ? -9 : 9, 6),
                );
                const tone = this.ctx.createGain();
                tone.gain.value = perNote * 0.7;
                osc.connect(tone);
                tone.connect(envelope);
                if (this.quality <= 0) {
                    const vibrato = this.ctx.createOscillator();
                    vibrato.frequency.value = 3.1 + index * 0.2;
                    const depth = this.ctx.createGain();
                    depth.gain.value = 10;
                    vibrato.connect(depth);
                    depth.connect(osc.detune);
                    vibrato.start(at);
                    vibrato.stop(at + Math.max(0.1, duration));
                }
            });
        }

        /** 低音：正弦 + 低通锯齿，短起音，负责和声根音与低频脉冲。 */
        bass(note: number, start: number, duration: number, gain: number): void {
            if (!Number.isFinite(note)) return;
            const filter = this.ctx.createBiquadFilter();
            filter.type = "lowpass";
            filter.frequency.setValueAtTime(320, start);
            filter.connect(this.host.out);
            const envelope = this.envelope(start, duration, gain, 0.08, 0.25);
            envelope.connect(filter);
            const sine = this.osc("sine", note, start, duration);
            const sub = this.ctx.createGain();
            sub.gain.value = 0.75;
            sine.connect(sub);
            sub.connect(envelope);
            if (this.quality <= 1) {
                const saw = this.osc("triangle", note, start, duration, 0);
                const sawGain = this.ctx.createGain();
                sawGain.gain.value = 0.28;
                saw.connect(sawGain);
                sawGain.connect(envelope);
            }
        }

        /** 拨弦/木琴：短促、干脆，用于 ostinato 与日常档位主题。 */
        pluck(note: number, start: number, gain: number): void {
            if (!Number.isFinite(note)) return;
            const at = this.startAt(start, 0.008);
            const osc = this.osc("triangle", note, at, 0.42, this.detune(0, 4));
            const envelope = this.ctx.createGain();
            envelope.gain.setValueAtTime(0.0001, at);
            envelope.gain.exponentialRampToValueAtTime(Math.max(0.0003, gain), at + 0.012);
            envelope.gain.exponentialRampToValueAtTime(0.0001, at + 0.4);
            osc.connect(envelope);
            envelope.connect(this.host.out);
            // 拨弦起音的短促点击，补足合成三角波缺失的瞬态。
            this.air(envelope, at, 0.055, gain * 0.3, "bandpass", 3600, 0.7);
        }

        /** 钟声：非整数倍频程 + 长衰减，用于 cue 钩子与留白。 */
        bell(note: number, start: number, gain: number): void {
            if (this.quality >= 2 || !Number.isFinite(note)) return;
            const base = this.ctx.createGain();
            base.gain.value = gain;
            base.connect(this.host.out);
            const partials: Array<{ ratio: number; level: number; decay: number }> = [
                { ratio: 1, level: 1, decay: 2.6 },
                { ratio: 2.76, level: 0.42, decay: 1.6 },
                { ratio: 5.4, level: 0.18, decay: 0.9 },
            ];
            partials.forEach((partial) => {
                const osc = this.osc("sine", note + 12 * Math.log2(partial.ratio), start, partial.decay, 0);
                const envelope = this.ctx.createGain();
                envelope.gain.setValueAtTime(0.0001, start);
                envelope.gain.exponentialRampToValueAtTime(Math.max(0.0003, partial.level), start + 0.02);
                envelope.gain.exponentialRampToValueAtTime(0.0001, start + partial.decay);
                osc.connect(envelope);
                envelope.connect(base);
            });
        }

        /** 定音鼓：下滑音高的低频正弦 + 低通噪声，撑起「心跳」。 */
        timpani(note: number, start: number, gain: number): void {
            if (!Number.isFinite(note)) return;
            const low = note + 0;
            const thump = this.osc("sine", low + 2, start, 1.4, 0);
            thump.frequency.setValueAtTime(midiToFreq(low + 2), start);
            thump.frequency.exponentialRampToValueAtTime(midiToFreq(low), start + 0.22);
            const env = this.ctx.createGain();
            env.gain.setValueAtTime(0.0001, start);
            env.gain.exponentialRampToValueAtTime(Math.max(0.0003, gain), start + 0.014);
            env.gain.exponentialRampToValueAtTime(0.0001, start + 1.1);
            thump.connect(env);
            env.connect(this.host.out);

            if (this.quality <= 1 && this.host.noise) {
                const source = this.ctx.createBufferSource();
                source.buffer = this.host.noise;
                const filter = this.ctx.createBiquadFilter();
                filter.type = "lowpass";
                filter.frequency.value = 220;
                const noiseEnv = this.ctx.createGain();
                noiseEnv.gain.setValueAtTime(0.0001, start);
                noiseEnv.gain.exponentialRampToValueAtTime(Math.max(0.0003, gain * 0.45), start + 0.02);
                noiseEnv.gain.exponentialRampToValueAtTime(0.0001, start + 0.3);
                source.connect(filter);
                filter.connect(noiseEnv);
                noiseEnv.connect(this.host.out);
                source.start(start);
                source.stop(start + 0.36);
            }
        }

        /** 次低频轰鸣：只负责压迫感，不参与和声。 */
        sub(note: number, start: number, duration: number, gain: number): void {
            if (this.quality >= 2 || !Number.isFinite(note)) return;
            const freq = this.ctx.createBiquadFilter();
            freq.type = "lowpass";
            freq.frequency.value = 90;
            freq.connect(this.host.out);
            const envelope = this.envelope(start, duration, gain, 0.8, 0.8);
            envelope.connect(freq);
            const osc = this.osc("sine", note - 12, start, duration, 0);
            osc.connect(envelope);
        }

        /** 噪声重音：高潮的「打击」与段落切换的吸气。 */
        noiseHit(start: number, duration: number, gain: number, frequency: number, type: BiquadFilterType): void {
            if (this.quality >= 2 || !this.host.noise) return;
            const source = this.ctx.createBufferSource();
            source.buffer = this.host.noise;
            source.loop = true;
            const offset = this.host.random() * Math.max(0.01, this.host.noise.duration - 1);
            const filter = this.ctx.createBiquadFilter();
            filter.type = type;
            filter.frequency.setValueAtTime(clampValue(frequency, 80, 8000), start);
            filter.Q.setValueAtTime(0.9, start);
            const env = this.ctx.createGain();
            env.gain.setValueAtTime(0.0001, start);
            env.gain.linearRampToValueAtTime(Math.max(0.0003, gain), start + Math.min(0.12, duration * 0.35));
            env.gain.exponentialRampToValueAtTime(0.0001, start + duration);
            source.connect(filter);
            filter.connect(env);
            env.connect(this.host.out);
            source.start(start, offset);
            source.stop(start + duration + 0.05);
        }

        /** 反向镲片式的长渐强，用于进入高潮前的最后一口「吸气」。 */
        swell(start: number, duration: number, gain: number): void {
            if (this.quality >= 1 || !this.host.noise) return;
            const source = this.ctx.createBufferSource();
            source.buffer = this.host.noise;
            source.loop = true;
            const filter = this.ctx.createBiquadFilter();
            filter.type = "highpass";
            filter.frequency.setValueAtTime(900, start);
            filter.frequency.linearRampToValueAtTime(280, start + duration);
            const env = this.ctx.createGain();
            env.gain.setValueAtTime(0.0001, start);
            env.gain.linearRampToValueAtTime(Math.max(0.0003, gain), start + duration);
            env.gain.exponentialRampToValueAtTime(0.0001, start + duration + 0.18);
            source.connect(filter);
            filter.connect(env);
            env.connect(this.host.out);
            source.start(start);
            source.stop(start + duration + 0.25);
        }
    }
}
