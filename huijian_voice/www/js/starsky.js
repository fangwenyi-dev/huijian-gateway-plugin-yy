/*
 * starsky.js —— 「星辰大海」星野生成器（v1.6.27，纯装饰层）
 *
 * 对齐小程序标准条 0d2200c5/901a18df 的工程做法：mulberry32 **固定种子**
 * PRNG 生成星点与行星群——种子不变则每次加载同一片天（小程序里 14 页
 * 共用同一片星空即靠这个机制）。零依赖、零网络、不碰 huijian.js 任何
 * 数据路径；容器缺失即整体静默跳过（防御式，装饰层永不反噬功能层）。
 *
 * 数量与参数均取标准终版：
 *  - 星点双层视差（far 细小慢闪 / near 稍亮），明灭单峰触零；
 *  - 行星 16 颗：3/4/6px 三档、辉光宁淡（α.35，晕 4~7px）、60% 压进
 *    左下星云区偏置；双层独立时钟：漂移 55~75s（≈1.5px/s 级）与明灭
 *    3.5~7s 解耦（耦合同一动画在标准评审里被否）。
 */
(function () {
    'use strict';

    // mulberry32：32bit 状态 PRNG，短小且跨引擎逐位确定
    function mulberry32(seed) {
        var a = seed >>> 0;
        return function () {
            a = (a + 0x6D2B79F5) >>> 0;
            var t = a;
            t = Math.imul(t ^ (t >>> 15), 1 | t);
            t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        };
    }

    // 固定种子＝本仓库的"那一片天"（改动会让全体用户换星空，勿动）
    var rnd = mulberry32(0x5EA7C0DE);
    function between(min, max) { return min + rnd() * (max - min); }
    function pick(arr) { return arr[Math.floor(rnd() * arr.length)]; }

    var SKY_SEED = 0x5EA7C0DE; // （文档性常量：回归测试锚定其存在）

    function el(cls, parent) {
        var n = document.createElement('div');
        n.className = cls;
        parent.appendChild(n);
        return n;
    }

    // v1.7.3 用户令对照官网母本（index.html L139-144 + fillStars L821）：
    // 星点 2/2.5/3px 三档（非旧 0.6~2.2px 微尘）、明暗 0.1↔0.9 正弦永不
    // 触零（非单峰长黑）、白60%/蓝#38bdf8 20%/琥珀#fbbf24 20%、大星带
    // 4px 白晕。行星群不动（小程序"单峰触零"口径仍适用其辉光）。
    function makeStars(containerId, count, sizeMin, sizeMax, durMin, durMax, peakMin, peakMax, glowFrom) {
        var host = document.getElementById(containerId);
        if (!host) return;
        var frag = document.createDocumentFragment();
        for (var i = 0; i < count; i++) {
            var s = document.createElement('i');
            var size = between(sizeMin, sizeMax).toFixed(2);
            var r = rnd();
            if (r >= 0.6) s.className = r < 0.8 ? 's-blue' : 's-amber';
            if (glowFrom && size >= glowFrom) s.className += ' s-glow';
            s.style.cssText =
                'left:' + (rnd() * 100).toFixed(2) + '%;' +
                'top:' + (rnd() * 100).toFixed(2) + '%;' +
                'width:' + size + 'px;height:' + size + 'px;' +
                '--dur:' + between(durMin, durMax).toFixed(2) + 's;' +
                '--delay:-' + between(0, durMax).toFixed(2) + 's;' +
                '--floor:' + (peakMin * 0.13).toFixed(2) + ';' +
                '--peak:' + between(peakMin, peakMax).toFixed(2) + ';';
            frag.appendChild(s);
        }
        host.appendChild(frag);
    }

    function makePlanets() {
        var host = document.getElementById('planets');
        if (!host) return;
        var palette = ['#7dd3fc', '#38bdf8', '#0ea5e9', '#e0f2fe'];
        var frag = document.createDocumentFragment();
        for (var i = 0; i < 16; i++) {
            var inNebula = (i % 10) < 6;   // 60% 压进星云团区（标准偏置）
            // v1.6.27 星云改官网三团口径后，偏置区随团重排：
            // 蓝团左上 / 金团中上 / 青团右下（::before/::after/.nebula3 落点）
            var x, y;
            if (inNebula) {
                var blob = i % 6;
                if (blob < 2)      { x = between(2, 42);  y = between(2, 45);  } // 蓝团
                else if (blob < 4) { x = between(55, 88); y = between(8, 48);  } // 金团
                else               { x = between(60, 97); y = between(42, 85); } // 青团
            } else {
                x = between(2, 96); y = between(2, 96);
            }
            var size = pick([3, 3, 4, 4, 6]);       // 三档，小星占多
            var drift = between(55, 75);            // 漂移一圈 55~75s
            var twk = between(3.5, 7);              // 明灭单周期 3.5~7s
            var ang = rnd() * Math.PI * 2;
            var dist = between(18, 42);             // 位移幅度→≈0.5~1.2px/s，宁慢
            var wrap = el('orbit-wrap', frag);
            wrap.style.cssText =
                'left:' + x.toFixed(2) + '%;top:' + y.toFixed(2) + '%;' +
                '--drift:' + drift.toFixed(1) + 's;' +
                '--delay:-' + between(0, drift).toFixed(1) + 's;' +
                '--dx:' + (Math.cos(ang) * dist).toFixed(1) + 'px;' +
                '--dy:' + (Math.sin(ang) * dist * 0.7).toFixed(1) + 'px;';
            var p = document.createElement('span');
            p.className = 'planet';
            p.style.cssText =
                '--sz:' + size + 'px;' +
                '--pc:' + pick(palette) + ';' +
                '--twk:' + twk.toFixed(2) + 's;' +
                '--blur:' + (size + between(1, 3)).toFixed(1) + 'px;' +   // 晕 4~7px 档
                '--halo:' + (size >= 6 ? 3 : 2) + 'px;' +
                '--peak:' + between(0.6, 0.95).toFixed(2) + ';';
            wrap.appendChild(p);
        }
        host.appendChild(frag);
    }

    // v1.7.4 用户令「多颗星星聚一团、明暗变化」——官网 canvas Star 大星
    // 口径的 CSS 对应物：4 团、每团 6~8 颗（半径 ±4.5%×6% 聚簇），每团
    // 1~2 颗核心大星带 8px 晕；正弦明暗/三色配比同星点。固定种子=同一片天。
    function makeClusters() {
        var host = document.getElementById('clusters');
        if (!host) return;
        var frag = document.createDocumentFragment();
        var centers = [[14, 47], [40, 55], [67, 44], [86, 62]];
        for (var c = 0; c < centers.length; c++) {
            var n = 6 + Math.floor(rnd() * 3);
            for (var i = 0; i < n; i++) {
                var s = document.createElement('i');
                var big = i === 0 || (i === 1 && rnd() < 0.6);
                var size = big ? between(2.6, 3.4) : between(1.4, 2.2);
                var r = rnd();
                s.className = (r >= 0.6 ? (r < 0.8 ? 's-blue' : 's-amber') : '') + (big ? ' s-glow' : '');
                s.style.cssText =
                    'left:' + (centers[c][0] + between(-4.5, 4.5)).toFixed(2) + '%;' +
                    'top:' + (centers[c][1] + between(-6, 6)).toFixed(2) + '%;' +
                    'width:' + size.toFixed(2) + 'px;height:' + size.toFixed(2) + 'px;' +
                    '--dur:' + between(3, 7).toFixed(2) + 's;' +
                    '--delay:-' + between(0, 7).toFixed(2) + 's;' +
                    '--floor:' + (big ? '0.15' : '0.10') + ';' +
                    '--peak:' + (big ? between(0.85, 1) : between(0.5, 0.8)).toFixed(2) + ';';
                frag.appendChild(s);
            }
        }
        host.appendChild(frag);
    }

    makeStars('starsFar', 70, 1.8, 2.5, 3, 7, 0.55, 0.75, 2.4);
    makeStars('starsNear', 36, 2.5, 3.2, 3, 7, 0.8, 0.95, 2.9);
    makeClusters();
    makePlanets();
})();
