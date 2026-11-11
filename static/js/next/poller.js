(function () {
    var registry = window.__bbPollers = window.__bbPollers || {};

    // Visibility- and activity-aware poller. run() returns true while work is
    // active (poll every fastMs), otherwise polling backs off to slowMs. A failed or
    // timed-out run (non-boolean result) keeps the previous state; run gets an AbortSignal.
    window.bbPoller = function (opts) {
        var timer = null, token = 0, active = false, running = false, rerun = false, stopped = false, fastUntil = 0;
        if (registry[opts.name]) registry[opts.name].stop();

        function interval() {
            return active || Date.now() < fastUntil ? opts.fastMs : opts.slowMs;
        }
        function schedule() {
            clearTimeout(timer);
            timer = null;
            if (!stopped && !document.hidden) timer = setTimeout(tick, interval());
        }
        function tick() {
            clearTimeout(timer);
            timer = null;
            if (stopped || document.hidden) return;
            if (running) { rerun = true; return; }
            running = true;
            var runId = ++token;
            var controller = typeof AbortController === 'function' ? new AbortController() : null;
            // Settles the run once; a late result from an abandoned run is ignored.
            function done(busy) {
                if (runId !== token) return;
                token++;
                clearTimeout(watchdog);
                if (typeof busy === 'boolean') active = busy;
                running = false;
                if (rerun) { rerun = false; tick(); } else schedule();
            }
            var watchdog = setTimeout(function () {
                if (controller) controller.abort();
                done();
            }, opts.timeoutMs || 25000);
            Promise.resolve().then(function () { return opts.run(controller && controller.signal); })
                .then(done, function () { done(); });
        }
        function onVisibility() {
            if (document.hidden) { clearTimeout(timer); timer = null; } else tick();
        }

        var poller = {
            // Work was just started: check now and stay on the fast interval for a while.
            kick: function () { fastUntil = Date.now() + (opts.graceMs || 60000); tick(); },
            stop: function () {
                stopped = true;
                clearTimeout(timer);
                timer = null;
                document.removeEventListener('visibilitychange', onVisibility);
                if (registry[opts.name] === poller) delete registry[opts.name];
            }
        };
        registry[opts.name] = poller;
        document.addEventListener('visibilitychange', onVisibility);
        if (opts.immediate === false) schedule(); else tick();
        return poller;
    };
})();
