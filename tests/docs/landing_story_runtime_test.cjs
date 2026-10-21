const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../../docs/landing-story.js'), 'utf8');
const scenes = ['research', 'create', 'automate', 'ship'];

function element(dataset = {}) {
    const attributes = {};
    const classes = new Set();
    return {
        dataset: { ...dataset },
        listeners: {},
        hidden: false,
        style: { values: {}, setProperty(name, value) { this.values[name] = value; } },
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
            contains: name => classes.has(name),
            toggle: (name, force) => (force ? classes.add(name) : classes.delete(name)),
        },
        addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
        emit(type, event = {}) { (this.listeners[type] || []).forEach(handler => handler(event)); },
        setAttribute(name, value) { attributes[name] = String(value); },
        getAttribute(name) { return attributes[name] ?? null; },
        removeAttribute(name) { delete attributes[name]; },
        focus() { this.focused = true; },
    };
}

// A video whose play() resolves and reports 'playing', as a browser does once frames flow.
function mediaElement({ refusePlay = false, startsAt = 0 } = {}) {
    const video = element();
    Object.assign(video, {
        paused: true,
        ended: false,
        currentTime: 0,
        duration: 8,
        playCalls: 0,
        load() {},
        play() {
            video.playCalls += 1;
            if (refusePlay) return Promise.reject(new Error('NotAllowedError'));
            if (video.paused) {
                video.paused = false;
                video.ended = false;
                video.currentTime = Math.max(video.currentTime, startsAt);
                video.emit('playing');
            }
            return Promise.resolve();
        },
        pause() { video.paused = true; },
        canPlayType: () => '',
    });
    Object.defineProperty(video, 'src', {
        get() { return video.getAttribute('src') || ''; },
        set(value) { video.setAttribute('src', value); },
    });
    return video;
}

function makePage({ reducedMotion = false, saveData = false, refusePlay = false, safari = false, buddyStartsAt = 1.5 } = {}) {
    const frame = element();
    const caption = element();
    const control = element();
    control.hidden = true;
    const buddy = element();
    const buddyStill = element();
    const buddyVideos = [0, 1].map(() => {
        const video = mediaElement({ startsAt: buddyStartsAt });
        if (safari) video.canPlayType = type => (type === 'video/mp4; codecs="hvc1"' ? 'probably' : '');
        return video;
    });

    const clips = {};
    const stills = {};
    const panels = scenes.map((scene, index) => {
        const panel = element({ scenePanel: scene });
        panel.hidden = index > 0;
        if (index === 0) panel.classList.add('is-shown');
        const clip = mediaElement({ refusePlay });
        clip.sources = [{ dataset: { src: `clips/${scene}.webm` } }, { dataset: { src: `clips/${scene}.mp4` } }];
        clip.querySelectorAll = () => clip.sources;
        clips[scene] = clip;
        const still = { loading: index === 0 ? 'eager' : 'lazy', decode: () => Promise.resolve() };
        stills[scene] = still;
        panel.querySelector = selector => (selector === 'video' ? clip : selector === 'img' ? still : null);
        return panel;
    });
    const tabs = scenes.map((scene, index) => {
        const tab = element({ sceneTab: scene });
        tab.setAttribute('aria-selected', String(index === 0));
        tab.tabIndex = index === 0 ? 0 : -1;
        tab.querySelector = () => ({ textContent: `${scene} line` });
        return tab;
    });

    const documentListeners = {};
    const document = {
        hidden: false,
        addEventListener(type, handler) { documentListeners[type] = handler; },
        querySelector(selector) {
            return {
                '[data-product-frame]': frame,
                '[data-scene-caption]': caption,
                '[data-product-control]': control,
                '[data-buddy]': buddy,
                '[data-buddy-still]': buddyStill,
            }[selector] ?? null;
        },
        querySelectorAll(selector) {
            if (selector === '[data-scene-tab]') return tabs;
            if (selector === '[data-scene-panel]') return panels;
            if (selector === '[data-buddy-video]') return buddyVideos;
            return [];
        },
    };
    const motionQuery = { matches: reducedMotion, listener: null, addEventListener(_, handler) { this.listener = handler; } };
    const frames = [];
    const timers = [];
    let observe = null;
    const window = {
        matchMedia: () => motionQuery,
        requestAnimationFrame: callback => frames.push(callback),
        setTimeout: (callback, delay) => timers.push({ callback, delay }),
        IntersectionObserver: class {
            constructor(callback) { observe = callback; }
            observe() {}
        },
    };
    class FakeImage { decode() { return Promise.resolve(); } }
    const navigator = {
        userAgent: safari
            ? 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'
            : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
        connection: { saveData },
    };
    vm.runInNewContext(source, {
        window, document, navigator, Image: FakeImage,
        IntersectionObserver: window.IntersectionObserver, Promise, String, Math,
    });

    return {
        frame, caption, control, buddy, buddyStill, buddyVideos, clips, stills, panels, tabs, document, motionQuery,
        scroll(ratio) { observe([{ intersectionRatio: ratio }]); },
        visibilityChanged() { documentListeners.visibilitychange(); },
        selected: () => tabs.find(tab => tab.getAttribute('aria-selected') === 'true').dataset.sceneTab,
        playing: () => scenes.filter(scene => !clips[scene].paused),
        shown: () => scenes.filter((scene, index) => !panels[index].hidden),
        buddyFront: () => buddyVideos.find(video => video.classList.contains('is-front') && video.classList.contains('is-top')),
        // Lets frames, promise callbacks and timers run, like a browser between paints.
        async settle() {
            for (let round = 0; round < 40; round += 1) {
                await new Promise(resolve => setImmediate(resolve));
                frames.splice(0).forEach(callback => callback());
                timers.splice(0).forEach(({ callback }) => callback());
            }
        },
        end(scene) {
            clips[scene].paused = true;
            clips[scene].ended = true;
            clips[scene].emit('ended');
        },
    };
}

async function run() {
    // The tour waits until the app window is at least half on screen, then plays once through.
    {
        const page = makePage();
        assert.deepEqual(page.playing(), [], 'nothing plays before the window is on screen');
        assert.equal(page.control.hidden, false);
        assert.equal(page.control.dataset.action, 'pause');
        page.scroll(.3);
        assert.deepEqual(page.playing(), []);
        page.scroll(.6);
        assert.deepEqual(page.playing(), ['research']);
        assert.deepEqual(page.clips.research.sources.map(item => item.src), ['clips/research.webm', 'clips/research.mp4']);
        await page.settle();
        assert.equal(page.buddyFront()?.src, 'media/landing-story/buddy/thinking.webm#t=1');
        assert.equal(page.buddy.classList.contains('is-live'), true);

        // While a scene plays, the next scene's still, clip and Buddy pose load in the background.
        assert.equal(page.stills.create.loading, 'eager');
        assert.equal(page.clips.create.sources[0].src, 'clips/create.webm');
        assert.equal(page.stills.automate.loading, 'lazy', 'only one scene ahead');
        assert.ok(page.buddyVideos.some(video => video.src === 'media/landing-story/buddy/working.webm'));

        // A scene change keeps the old scene on screen until the new one is ready, then crossfades.
        page.clips.create.play = function play() {
            this.paused = false;
            return Promise.resolve();  // frames not flowing yet
        };
        page.end('research');
        assert.equal(page.selected(), 'create');
        assert.equal(page.caption.textContent, 'create line');
        assert.deepEqual(page.shown(), ['research', 'create']);
        assert.equal(page.panels[0].classList.contains('is-leaving'), true);
        assert.equal(page.panels[1].classList.contains('is-shown'), false, 'not before its first frame');
        page.clips.create.emit('playing');
        await page.settle();
        assert.equal(page.panels[1].classList.contains('is-shown'), true);
        assert.deepEqual(page.shown(), ['create'], 'the old scene goes once the fade ends');
        assert.deepEqual(page.playing(), ['create']);
        assert.equal(page.buddyFront()?.src, 'media/landing-story/buddy/working.webm');

        page.scroll(0);
        assert.deepEqual(page.playing(), [], 'scrolling away pauses the clip');
        assert.ok(page.buddyVideos.every(video => video.paused), 'and Buddy');
        page.scroll(1);
        assert.deepEqual(page.playing(), ['create']);

        page.document.hidden = true;
        page.visibilityChanged();
        assert.deepEqual(page.playing(), [], 'a hidden browser tab pauses the clip');
        page.document.hidden = false;
        page.visibilityChanged();
        assert.deepEqual(page.playing(), ['create']);

        page.end('create');
        page.end('automate');
        await page.settle();
        assert.equal(page.selected(), 'ship');
        assert.equal(page.buddy.dataset.state, 'approval');
        page.clips.ship.currentTime = 2.3;
        page.clips.ship.emit('timeupdate');
        assert.equal(page.buddy.dataset.state, 'success', 'Buddy celebrates once the approval clears');
        assert.equal(page.tabs[3].style.values['--progress'], String(2.3 / 8));

        page.end('ship');
        await page.settle();
        assert.deepEqual(page.playing(), [], 'the tour stops after one pass');
        assert.equal(page.selected(), 'ship');
        assert.equal(page.control.dataset.action, 'replay');
        assert.equal(page.control.getAttribute('aria-label'), 'Replay the product tour');
        assert.equal(page.buddyFront()?.loop, false, 'success plays once');

        page.control.emit('click');
        assert.equal(page.selected(), 'research');
        assert.deepEqual(page.playing(), ['research']);
        assert.equal(page.control.dataset.action, 'pause');
    }

    // Buddy's first frames can decode without alpha, so a new pose waits to swap in.
    {
        const page = makePage({ buddyStartsAt: .05 });
        page.scroll(1);
        await page.settle();
        assert.equal(page.buddyFront(), undefined, 'not while the pose is still at its first frames');
        assert.equal(page.buddy.classList.contains('is-live'), false, 'the still stays up');
        const loading = page.buddyVideos.find(video => !video.paused);
        loading.currentTime = 1.25;
        await page.settle();
        assert.equal(page.buddyFront(), loading);
    }

    // Pause and play are one icon button; tabs and arrow keys jump between scenes.
    {
        const page = makePage();
        page.scroll(1);
        page.control.emit('click');
        assert.deepEqual(page.playing(), []);
        assert.ok(page.buddyVideos.every(video => video.paused));
        assert.equal(page.control.dataset.action, 'play');
        assert.equal(page.control.title, 'Play');
        page.control.emit('click');
        assert.deepEqual(page.playing(), ['research']);

        page.tabs[2].emit('click');
        await page.settle();
        assert.equal(page.selected(), 'automate');
        assert.deepEqual(page.playing(), ['automate']);
        assert.deepEqual(page.shown(), ['automate']);
        assert.equal(page.tabs[2].tabIndex, 0);
        assert.equal(page.tabs[0].tabIndex, -1);
        let prevented = false;
        page.tabs[2].emit('keydown', { key: 'ArrowRight', preventDefault() { prevented = true; } });
        assert.ok(prevented);
        assert.equal(page.selected(), 'ship');
        assert.equal(page.tabs[3].focused, true);
        page.tabs[3].emit('keydown', { key: 'ArrowRight', preventDefault() {} });
        assert.equal(page.selected(), 'research', 'arrow keys wrap around');
        page.tabs[0].emit('keydown', { key: 'End', preventDefault() {} });
        assert.equal(page.selected(), 'ship');
    }

    // Reduced motion and Save-Data: nothing moves until asked, and a requested clip does not advance.
    for (const options of [{ reducedMotion: true }, { saveData: true }]) {
        const page = makePage(options);
        page.scroll(1);
        await page.settle();
        assert.deepEqual(page.playing(), []);
        assert.equal(page.control.dataset.action, 'play');
        assert.ok(page.buddyVideos.every(video => video.src === ''), 'Buddy stays a still');
        page.tabs[1].emit('click');
        await page.settle();
        assert.equal(page.selected(), 'create');
        assert.deepEqual(page.shown(), ['create'], 'a tab shows the still');
        assert.deepEqual(page.playing(), []);
        assert.equal(page.buddyStill.src, 'media/landing-story/buddy/working.webp');
        assert.equal(page.clips.automate.sources[0].src, undefined, 'nothing preloads');
        page.control.emit('click');
        assert.deepEqual(page.playing(), ['create']);
        page.end('create');
        assert.equal(page.selected(), 'create', 'no advance without motion');
        assert.equal(page.control.dataset.action, 'play');
    }

    // Turning on reduced motion mid-tour stops it.
    {
        const page = makePage();
        page.scroll(1);
        page.motionQuery.matches = true;
        page.motionQuery.listener();
        assert.deepEqual(page.playing(), []);
        assert.equal(page.control.dataset.action, 'play');
    }

    // A refused autoplay keeps the still and offers Play.
    {
        const page = makePage({ refusePlay: true });
        page.scroll(1);
        await page.settle();
        assert.equal(page.control.dataset.action, 'play');
        assert.deepEqual(page.playing(), []);
    }

    // Apple WebKit drops WebM alpha, so Buddy uses HEVC with alpha there.
    {
        const page = makePage({ safari: true });
        page.scroll(1);
        await page.settle();
        assert.equal(page.buddyFront()?.src, 'media/landing-story/buddy/thinking.mov#t=1');
    }
}

run().catch(error => {
    console.error(error);
    process.exit(1);
});
