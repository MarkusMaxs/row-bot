(() => {
    'use strict';

    const SCENES = ['research', 'create', 'automate', 'ship'];
    const BUDDY_STATES = { research: 'thinking', create: 'working', automate: 'idle', ship: 'approval' };
    const BUDDY_MEDIA = 'media/landing-story/buddy/';
    // Thinking opens on a wind-up; its loop reads well from one second in.
    const BUDDY_START = { thinking: 1 };
    // The reviewed Ship cut clears its approval dialog at 2.25 seconds.
    const SHIP_APPROVAL_AT = 2.25;
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
    const buddyVideo = document.querySelector('[data-buddy-video]');

    let scene = 'research';
    // The tour plays once through on its own; reduced motion and Save-Data wait for Play.
    let running = !reducedMotion.matches && !saveData;
    let finished = false;
    let visible = !('IntersectionObserver' in window);
    let buddyState = '';

    const panelFor = name => panels.find(panel => panel.dataset.scenePanel === name);
    const clipFor = name => panelFor(name)?.querySelector('video');
    const tabFor = name => tabs.find(tab => tab.dataset.sceneTab === name);
    const onScreen = () => visible && !document.hidden;
    const motionAllowed = () => !reducedMotion.matches && !saveData;

    function afterFirstFrame(video, callback) {
        if ('requestVideoFrameCallback' in video) video.requestVideoFrameCallback(() => callback());
        else window.requestAnimationFrame(() => callback());
    }

    function hydrate(video) {
        if (video.dataset.hydrated) return;
        video.dataset.hydrated = 'true';
        video.querySelectorAll('source').forEach(source => { source.src = source.dataset.src; });
        video.load();
    }

    function show(name) {
        if (name === scene) return;
        clipFor(scene)?.pause();
        scene = name;
        frame.dataset.scene = name;
        panels.forEach(panel => {
            panel.hidden = panel.dataset.scenePanel !== name;
            panel.classList.remove('is-live');
        });
        tabs.forEach(tab => {
            const selected = tab.dataset.sceneTab === name;
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected ? 0 : -1;
            tab.style.setProperty('--progress', '0');
        });
        if (caption) caption.textContent = tabFor(name)?.querySelector('.scene-tab__line')?.textContent || '';
        const clip = clipFor(name);
        if (clip?.dataset.hydrated) {
            try { clip.currentTime = 0; } catch (_) { /* Metadata may not be ready. */ }
        }
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
        if (appleWebKit && buddyVideo.canPlayType?.(HEVC_ALPHA_TYPE)) return `${BUDDY_MEDIA}${state}.mov`;
        return `${BUDDY_MEDIA}${state}.webm`;
    }

    function syncBuddy(animate) {
        if (!buddy || !buddyVideo) return;
        const clip = clipFor('ship');
        const approved = scene === 'ship' && (finished || (clip && clip.currentTime >= SHIP_APPROVAL_AT));
        const state = approved ? 'success' : BUDDY_STATES[scene];
        if (state !== buddyState) {
            buddyState = state;
            buddy.dataset.state = state;
            buddy.classList.remove('is-live');
            buddyStill.src = `${BUDDY_MEDIA}${state}.webp`;
            buddyVideo.pause();
            buddyVideo.removeAttribute('src');
            buddyVideo.hidden = true;
        }
        if (!animate) {
            buddyVideo.pause();
            return;
        }
        if (!buddyVideo.getAttribute('src')) {
            buddyVideo.loop = state !== 'success';
            buddyVideo.src = `${buddySource(state)}${BUDDY_START[state] ? `#t=${BUDDY_START[state]}` : ''}`;
            buddyVideo.hidden = false;
        }
        // Success plays once and then rests on its last frame.
        if (buddyVideo.paused && !buddyVideo.ended) {
            buddyVideo.play().catch(() => { buddyVideo.hidden = true; });
        }
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
        show(name);
        if (motionAllowed()) running = true;
        if (focus) tabFor(name)?.focus();
        sync();
    }

    panels.forEach(panel => {
        const video = panel.querySelector('video');
        if (!video) return;
        video.addEventListener('playing', () => afterFirstFrame(video, () => panel.classList.add('is-live')));
        video.addEventListener('timeupdate', () => {
            if (panel.dataset.scenePanel !== scene || !video.duration) return;
            tabFor(scene)?.style.setProperty('--progress', String(Math.min(1, video.currentTime / video.duration)));
            if (scene === 'ship') syncBuddy(onScreen() && motionAllowed() && running);
        });
        video.addEventListener('ended', () => {
            if (panel.dataset.scenePanel !== scene) return;
            const next = SCENES[SCENES.indexOf(scene) + 1];
            if (!next) {
                finished = true;
                running = false;
            } else if (motionAllowed()) show(next);
            else running = false;
            sync();
        });
    });

    buddyVideo?.addEventListener('playing', () => afterFirstFrame(buddyVideo, () => buddy.classList.add('is-live')));
    buddyVideo?.addEventListener('error', () => {
        buddyVideo.hidden = true;
        buddy.classList.remove('is-live');
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
            show(SCENES[0]);
            running = true;
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
