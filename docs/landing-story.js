(() => {
    'use strict';

    const SCENES = ['research', 'create', 'automate', 'ship'];
    const BUDDY_STATES = { research: 'thinking', create: 'working', automate: 'idle', ship: 'approval' };
    const BUDDY_MEDIA = 'media/landing-story/buddy/';
    // Thinking opens on a wind-up; its loop reads well from one second in.
    const BUDDY_START = { thinking: 1 };
    // The reviewed Ship cut clears its approval dialog at 2.25 seconds.
    const SHIP_APPROVAL_AT = 2.25;
    const BUDDY_CLEAN_AFTER = .2;
    // Matches the .scene opacity transition in landing-story.css.
    const CROSSFADE_MS = 320;
    // How long a scene change waits for the next clip's first frame before showing its still.
    const CLIP_WAIT_MS = 1200;
    const HEVC_ALPHA_TYPE = 'video/mp4; codecs="hvc1"';
    const CONTROLS = {
        pause: { label: 'Pause the product tour', icon: '<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/>' },
        play: { label: 'Play the product tour', icon: '<path d="M8 5.5v13l10.5-6.5z"/>' },
        replay: { label: 'Replay the product tour', icon: '<path d="M12 5a7 7 0 1 1-6.93 8h2.02A5 5 0 1 0 12 7v3L7.5 6 12 2z"/>' }
    };

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const saveData = Boolean(navigator.connection?.saveData);
    // Safari and every iOS browser (Apple WebKit) play WebM but drop its alpha
    // channel, so Buddy would sit in a dark box; they get HEVC with alpha.
    const appleWebKit = /AppleWebKit/.test(navigator.userAgent || '')
        && !/(Chrome|Chromium|Edg|OPR)\//.test(navigator.userAgent || '');

    const demoPosters = [...document.querySelectorAll('[data-demo-poster]')];
    if ('IntersectionObserver' in window) {
        const posterObserver = new IntersectionObserver(entries => {
            entries.forEach(entry => {
                if (!entry.isIntersecting) return;
                entry.target.src = entry.target.dataset.src;
                posterObserver.unobserve(entry.target);
            });
        }, { rootMargin: '80px 0px' });
        demoPosters.forEach(image => posterObserver.observe(image));
    } else demoPosters.forEach(image => { image.src = image.dataset.src; });

    const frame = document.querySelector('[data-product-frame]');
    if (!frame) return;
    const tabs = [...document.querySelectorAll('[data-scene-tab]')];
    const panels = [...document.querySelectorAll('[data-scene-panel]')];
    const caption = document.querySelector('[data-scene-caption]');
    const control = document.querySelector('[data-product-control]');
    const buddy = document.querySelector('[data-buddy]');
    const buddyStill = document.querySelector('[data-buddy-still]');
    // Two video slots: the next pose loads in one while the other keeps playing.
    const buddyVideos = [...document.querySelectorAll('[data-buddy-video]')];

    let scene = 'research';
    // The tour plays once through on its own; reduced motion and Save-Data wait for Play.
    let running = !reducedMotion.matches && !saveData;
    let finished = false;
    let visible = !('IntersectionObserver' in window);
    let buddyFront = null;

    const panelFor = name => panels.find(panel => panel.dataset.scenePanel === name);
    const clipFor = name => panelFor(name)?.querySelector('video');
    const tabFor = name => tabs.find(tab => tab.dataset.sceneTab === name);
    const nextScene = () => SCENES[SCENES.indexOf(scene) + 1];
    const onScreen = () => visible && !document.hidden;
    const motionAllowed = () => !reducedMotion.matches && !saveData;

    function afterFirstFrame(video, callback) {
        if ('requestVideoFrameCallback' in video) video.requestVideoFrameCallback(() => callback());
        else window.requestAnimationFrame(() => callback());
    }

    function hydrate(video) {
        if (video.dataset.hydrated) return;
        video.dataset.hydrated = 'true';
        video.preload = 'auto';
        video.querySelectorAll('source').forEach(source => { source.src = source.dataset.src; });
        video.load();
    }

    // The incoming scene fades in over the outgoing one, which stays on its last
    // frame until the fade ends, so a scene change never shows an empty screen.
    function reveal(panel) {
        if (panel.dataset.scenePanel !== scene || panel.classList.contains('is-shown')) return;
        panel.classList.add('is-shown');
        window.setTimeout(() => {
            panels.forEach(other => {
                if (other === panelFor(scene) || !other.classList.contains('is-leaving')) return;
                other.classList.remove('is-leaving', 'is-live');
                other.hidden = true;
            });
        }, CROSSFADE_MS);
    }

    function show(name) {
        if (name === scene) return;
        const outgoing = panelFor(scene);
        clipFor(scene)?.pause();
        scene = name;
        frame.dataset.scene = name;
        tabs.forEach(tab => {
            const selected = tab.dataset.sceneTab === name;
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected ? 0 : -1;
            tab.style.setProperty('--progress', '0');
        });
        if (caption) caption.textContent = tabFor(name)?.querySelector('.scene-tab__line')?.textContent || '';

        const incoming = panelFor(name);
        if (outgoing.classList.contains('is-shown')) {
            outgoing.classList.remove('is-shown');
            outgoing.classList.add('is-leaving');
        } else outgoing.hidden = true;
        incoming.classList.remove('is-leaving', 'is-shown', 'is-live');
        incoming.hidden = false;
        // Commit the transparent start, or a preloaded clip's first frame can land
        // before the browser draws it and the fade becomes a cut.
        void incoming.offsetWidth;
        const clip = clipFor(name);
        if (clip?.dataset.hydrated) {
            try { clip.currentTime = 0; } catch (_) { /* Metadata may not be ready. */ }
        }
        // A playing tour waits briefly for the clip's first frame; otherwise the still shows once decoded.
        const still = incoming.querySelector('img');
        still.loading = 'eager';
        const waitForClip = running && onScreen();
        Promise.resolve(still.decode?.()).catch(() => {}).then(() => {
            window.setTimeout(() => reveal(incoming), waitForClip ? CLIP_WAIT_MS : 0);
        });
    }

    function setControl(action) {
        if (!control) return;
        control.hidden = false;
        control.dataset.action = action;
        control.setAttribute('aria-label', CONTROLS[action].label);
        control.title = CONTROLS[action].label.replace(' the product tour', '');
        control.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">${CONTROLS[action].icon}</svg>`;
    }

    function buddySource(state) {
        const hevc = appleWebKit && buddyVideos[0].canPlayType?.(HEVC_ALPHA_TYPE);
        return `${BUDDY_MEDIA}${state}.${hevc ? 'mov' : 'webm'}${BUDDY_START[state] ? `#t=${BUDDY_START[state]}` : ''}`;
    }

    function buddyStateNow() {
        const clip = clipFor('ship');
        const approved = scene === 'ship' && (finished || (clip && clip.currentTime >= SHIP_APPROVAL_AT));
        return approved ? 'success' : BUDDY_STATES[scene];
    }

    const upcomingBuddyState = () => (nextScene() ? BUDDY_STATES[nextScene()] : 'success');

    // Loads a pose into the slot that is not on screen, ready to swap in.
    function prepareBuddy(state) {
        const ready = buddyVideos.find(video => video.dataset.state === state);
        if (ready) return ready;
        // Never the pose on screen, nor the one about to replace it.
        const slot = buddyVideos.find(video => video !== buddyFront && video.dataset.state !== buddy.dataset.state);
        if (!slot) return null;
        slot.dataset.state = state;
        slot.loop = state !== 'success';
        slot.preload = 'auto';
        slot.src = buddySource(state);
        return slot;
    }

    function showBuddyStill(state) {
        buddyVideos.forEach(video => video.pause());
        if (buddyFront?.dataset.state === state) return;
        const source = `${BUDDY_MEDIA}${state}.webp`;
        const next = new Image();
        next.src = source;
        Promise.resolve(next.decode?.()).catch(() => {}).then(() => {
            if (buddy.dataset.state !== state) return;
            buddyStill.src = source;
            buddyFront?.classList.remove('is-front');
            buddyFront = null;
            buddy.classList.remove('is-live');
        });
    }

    function syncBuddy(animate) {
        if (!buddy) return;
        const state = buddyStateNow();
        buddy.dataset.state = state;
        if (!animate || !buddyVideos.length) {
            showBuddyStill(state);
            return;
        }
        const target = prepareBuddy(state);
        if (!target) return;
        // Success plays once and then rests on its last frame; a later pass plays it again.
        if (target.paused && (!target.ended || target !== buddyFront)) target.play().catch(() => {});
        if (target !== buddyFront) buddyFront?.play().catch(() => {});
    }

    function promoteBuddy(video) {
        if (video.dataset.state !== buddy.dataset.state || video === buddyFront) return;
        const previous = buddyFront;
        video.classList.add('is-front', 'is-top');
        previous?.classList.remove('is-top');
        buddyFront = video;
        buddy.classList.add('is-live');
        if (!previous) return;
        // The browser needs a frame to paint the incoming video; keep the old pose until then.
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
            if (previous === buddyFront) return;
            previous.classList.remove('is-front');
            previous.pause();
            if (motionAllowed()) prepareBuddy(upcomingBuddyState());
        }));
    }

    function sync() {
        const clip = clipFor(scene);
        const playing = running && onScreen();
        panels.forEach(panel => {
            const video = panel.querySelector('video');
            if (video !== clip || !playing) video?.pause();
        });
        if (playing && clip) {
            hydrate(clip);
            clip.play().catch(() => {
                // Autoplay refused or the clip failed: keep the still and offer Play.
                running = false;
                sync();
            });
        }
        syncBuddy(onScreen() && motionAllowed() && (playing || finished));
        frame.classList.toggle('is-playing', playing);
        setControl(finished ? 'replay' : running ? 'pause' : 'play');
    }

    function select(name, { focus = false } = {}) {
        finished = false;
        if (motionAllowed()) running = true;
        show(name);
        if (focus) tabFor(name)?.focus();
        sync();
    }

    panels.forEach(panel => {
        const video = panel.querySelector('video');
        if (!video) return;
        video.addEventListener('playing', () => afterFirstFrame(video, () => {
            panel.classList.add('is-live');
            reveal(panel);
            if (panel.dataset.scenePanel !== scene || !motionAllowed()) return;
            // Load the next scene while this one plays, so the change is seamless.
            const next = nextScene();
            if (next) {
                panelFor(next).querySelector('img').loading = 'eager';
                hydrate(clipFor(next));
            }
            prepareBuddy(upcomingBuddyState());
        }));
        video.addEventListener('timeupdate', () => {
            if (panel.dataset.scenePanel !== scene || !video.duration) return;
            tabFor(scene)?.style.setProperty('--progress', String(Math.min(1, video.currentTime / video.duration)));
            if (scene === 'ship') syncBuddy(onScreen() && motionAllowed() && running);
        });
        video.addEventListener('ended', () => {
            if (panel.dataset.scenePanel !== scene) return;
            const next = nextScene();
            if (!next) {
                finished = true;
                running = false;
            } else if (motionAllowed()) show(next);
            else running = false;
            sync();
        });
    });

    // The first frames of a freshly started alpha video can decode without their
    // alpha (a dark box), so a pose swaps in only once it is a little way in.
    function promoteWhenClean(video) {
        const cleanAt = (BUDDY_START[video.dataset.state] || 0) + BUDDY_CLEAN_AFTER;
        const check = (_, metadata) => {
            if (video.paused) return;
            if ((metadata?.mediaTime ?? video.currentTime) >= cleanAt) promoteBuddy(video);
            else next();
        };
        const next = () => {
            if ('requestVideoFrameCallback' in video) video.requestVideoFrameCallback(check);
            else window.requestAnimationFrame(() => check());
        };
        next();
    }

    buddyVideos.forEach(video => {
        video.addEventListener('playing', () => promoteWhenClean(video));
        video.addEventListener('error', () => {
            video.removeAttribute('src');
            delete video.dataset.state;
            if (video === buddyFront) showBuddyStill(buddy.dataset.state);
        });
    });

    tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => select(tab.dataset.sceneTab));
        tab.addEventListener('keydown', event => {
            const target = event.key === 'ArrowRight' ? index + 1
                : event.key === 'ArrowLeft' ? index - 1
                : event.key === 'Home' ? 0
                : event.key === 'End' ? tabs.length - 1 : null;
            if (target === null) return;
            event.preventDefault();
            select(tabs[(target + tabs.length) % tabs.length].dataset.sceneTab, { focus: true });
        });
    });

    control?.addEventListener('click', () => {
        if (finished) {
            finished = false;
            running = true;
            show(SCENES[0]);
        } else running = !running;
        sync();
    });

    if ('IntersectionObserver' in window) {
        new IntersectionObserver(entries => {
            visible = entries[0].intersectionRatio >= .5;
            sync();
        }, { threshold: [0, .5] }).observe(frame);
    }
    document.addEventListener('visibilitychange', sync);
    reducedMotion.addEventListener?.('change', () => {
        if (reducedMotion.matches) running = false;
        sync();
    });

    sync();
})();
