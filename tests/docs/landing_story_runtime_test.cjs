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
            add: name => classes.add(name),
            remove: name => classes.delete(name),
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

function mediaElement(dataset = {}, { refusePlay = false } = {}) {
    const video = element(dataset);
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
            video.paused = false;
            video.ended = false;
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

function makePage({ reducedMotion = false, saveData = false, refusePlay = false, safari = false } = {}) {
    const frame = element();
    const caption = element();
    const control = element();
    control.hidden = true;
    const buddy = element();
    const buddyStill = element();
    const buddyVideo = mediaElement();
    buddyVideo.hidden = true;
    if (safari) buddyVideo.canPlayType = type => (type === 'video/mp4; codecs="hvc1"' ? 'probably' : '');

    const clips = {};
    const panels = scenes.map((scene, index) => {
        const panel = element({ scenePanel: scene });
        panel.hidden = index > 0;
        const clip = mediaElement({}, { refusePlay });
        clip.sources = [{ dataset: { src: `clips/${scene}.webm` } }, { dataset: { src: `clips/${scene}.mp4` } }];
        clip.querySelectorAll = () => clip.sources;
        clips[scene] = clip;
        panel.querySelector = selector => (selector === 'video' ? clip : null);
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
                '[data-buddy-video]': buddyVideo,
            }[selector] ?? null;
        },
        querySelectorAll(selector) {
            if (selector === '[data-scene-tab]') return tabs;
            if (selector === '[data-scene-panel]') return panels;
            return [];
        },
    };
    const motionQuery = { matches: reducedMotion, listener: null, addEventListener(_, handler) { this.listener = handler; } };
    let observe = null;
    const window = {
        matchMedia: () => motionQuery,
        requestAnimationFrame: callback => callback(),
        IntersectionObserver: class {
            constructor(callback) { observe = callback; }
            observe() {}
        },
    };
    const navigator = {
        userAgent: safari
            ? 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'
            : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
        connection: { saveData },
    };
    vm.runInNewContext(source, { window, document, navigator, IntersectionObserver: window.IntersectionObserver, Promise, String, Math });

    const page = {
        frame, caption, control, buddy, buddyStill, buddyVideo, clips, panels, tabs, document, motionQuery,
        scroll(ratio) { observe([{ intersectionRatio: ratio }]); },
        visibilityChanged() { documentListeners.visibilitychange(); },
        selected: () => tabs.find(tab => tab.getAttribute('aria-selected') === 'true').dataset.sceneTab,
        playing: () => scenes.filter(scene => !clips[scene].paused),
        end(scene) {
            clips[scene].paused = true;
            clips[scene].ended = true;
            clips[scene].emit('ended');
        },
    };
    return page;
}

const settle = () => new Promise(resolve => setImmediate(resolve));

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
        assert.equal(page.clips.create.sources[0].src, undefined, 'later clips load only when reached');
        assert.equal(page.buddyVideo.src, 'media/landing-story/buddy/thinking.webm#t=1');
        assert.equal(page.buddyVideo.paused, false);

        page.end('research');
        assert.equal(page.selected(), 'create');
        assert.equal(page.panels[0].hidden, true);
        assert.equal(page.panels[1].hidden, false);
        assert.equal(page.caption.textContent, 'create line');
        assert.deepEqual(page.playing(), ['create']);
        assert.equal(page.buddy.dataset.state, 'working');

        page.scroll(0);
        assert.deepEqual(page.playing(), [], 'scrolling away pauses the clip');
        assert.equal(page.buddyVideo.paused, true, 'and Buddy');
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
        assert.equal(page.selected(), 'ship');
        assert.equal(page.buddy.dataset.state, 'approval');
        page.clips.ship.currentTime = 2.3;
        page.clips.ship.emit('timeupdate');
        assert.equal(page.buddy.dataset.state, 'success', 'Buddy celebrates once the approval clears');
        assert.equal(page.tabs[3].style.values['--progress'], String(2.3 / 8));

        page.end('ship');
        assert.deepEqual(page.playing(), [], 'the tour stops after one pass');
        assert.equal(page.selected(), 'ship');
        assert.equal(page.control.dataset.action, 'replay');
        assert.equal(page.control.getAttribute('aria-label'), 'Replay the product tour');
        assert.equal(page.buddyVideo.loop, false, 'success plays once');

        page.control.emit('click');
        assert.equal(page.selected(), 'research');
        assert.deepEqual(page.playing(), ['research']);
        assert.equal(page.control.dataset.action, 'pause');
    }

    // Pause and play are one icon button; tabs and arrow keys jump between scenes.
    {
        const page = makePage();
        page.scroll(1);
        page.control.emit('click');
        assert.deepEqual(page.playing(), []);
        assert.equal(page.buddyVideo.paused, true);
        assert.equal(page.control.dataset.action, 'play');
        assert.equal(page.control.title, 'Play');
        page.control.emit('click');
        assert.deepEqual(page.playing(), ['research']);

        page.tabs[2].emit('click');
        assert.equal(page.selected(), 'automate');
        assert.deepEqual(page.playing(), ['automate']);
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

    // Reduced motion: nothing moves until asked, and a requested clip does not advance.
    for (const options of [{ reducedMotion: true }, { saveData: true }]) {
        const page = makePage(options);
        page.scroll(1);
        assert.deepEqual(page.playing(), []);
        assert.equal(page.control.dataset.action, 'play');
        assert.equal(page.buddyVideo.src, '', 'Buddy stays a still');
        page.tabs[1].emit('click');
        assert.equal(page.selected(), 'create');
        assert.deepEqual(page.playing(), [], 'a tab shows the still');
        assert.equal(page.buddy.dataset.state, 'working');
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
        await settle();
        assert.equal(page.control.dataset.action, 'play');
        assert.deepEqual(page.playing(), []);
    }

    // Apple WebKit drops WebM alpha, so Buddy uses HEVC with alpha there.
    {
        const page = makePage({ safari: true });
        page.scroll(1);
        assert.equal(page.buddyVideo.src, 'media/landing-story/buddy/thinking.mov#t=1');
    }
}

run().catch(error => {
    console.error(error);
    process.exit(1);
});
