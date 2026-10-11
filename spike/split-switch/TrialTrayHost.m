// Phase 1 spike (throwaway): the macOS tray host, extended with the PLAN.md
// layout contract. It reads ~/Library/Application Support/RowBotSplitTrial/state.json,
// applies the rollback rule, starts versions/<active>/python (or the bundle's
// seed), clears pending_verification once /readyz answers, and relaunches with
// the same arguments when the app asks for a restart ("restart": true).
//
// Extra entry points for the trial:
//   --trial-probe <out.json>   run Resources/trial/probe.py with the active
//                              version's Python and exit (CI evidence)
//   menu "Trial: ..." items     the manual-check kit (mic/camera tests from the
//                              active Python, stage an update, restart to update)
#import <Cocoa/Cocoa.h>
#import <Foundation/Foundation.h>
#import <dispatch/dispatch.h>
#import <signal.h>

static NSString * const RowBotAppName = @"Row-Bot";
static NSString * const TrialRootName = @"RowBotSplitTrial";
static NSInteger const RowBotMaxLaunches = 6;
static NSInteger const RowBotMaxFailedStarts = 2;

static NSString *TrialRoot(void) {
    NSString *base = [NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, YES) firstObject];
    return [base stringByAppendingPathComponent:TrialRootName];
}

static NSString *RowBotHomeDataDir(void) {
    NSString *envDataDir = [[[NSProcessInfo processInfo] environment] objectForKey:@"ROW_BOT_DATA_DIR"];
    if (envDataDir.length > 0) {
        return [envDataDir stringByExpandingTildeInPath];
    }
    NSString *trialData = [TrialRoot() stringByAppendingPathComponent:@"data"];
    return trialData;
}

static void EnsureDir(NSString *path) {
    [[NSFileManager defaultManager] createDirectoryAtPath:path withIntermediateDirectories:YES attributes:nil error:nil];
}

static void TrialLog(NSString *format, ...) {
    EnsureDir(TrialRoot());
    NSString *logPath = [TrialRoot() stringByAppendingPathComponent:@"host.log"];
    va_list args;
    va_start(args, format);
    NSString *message = [[NSString alloc] initWithFormat:format arguments:args];
    va_end(args);
    NSString *line = [NSString stringWithFormat:@"%.3f pid=%d %@\n", [[NSDate date] timeIntervalSince1970], getpid(), message];
    NSFileHandle *handle = [NSFileHandle fileHandleForWritingAtPath:logPath];
    if (!handle) {
        [line writeToFile:logPath atomically:NO encoding:NSUTF8StringEncoding error:nil];
        return;
    }
    @try {
        [handle seekToEndOfFile];
        [handle writeData:[line dataUsingEncoding:NSUTF8StringEncoding]];
    } @catch (__unused NSException *exception) {
    } @finally {
        [handle closeFile];
    }
}

static NSMutableDictionary *ReadState(void) {
    NSString *path = [TrialRoot() stringByAppendingPathComponent:@"state.json"];
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (!data) {
        return nil;
    }
    id json = [NSJSONSerialization JSONObjectWithData:data options:NSJSONReadingMutableContainers error:nil];
    if (![json isKindOfClass:[NSDictionary class]]) {
        return nil;
    }
    NSMutableDictionary *state = (NSMutableDictionary *)json;
    if ([[state objectForKey:@"schema"] integerValue] != 1) {
        return nil;
    }
    return state;
}

static BOOL WriteState(NSDictionary *state) {
    EnsureDir(TrialRoot());
    NSString *path = [TrialRoot() stringByAppendingPathComponent:@"state.json"];
    NSData *data = [NSJSONSerialization dataWithJSONObject:state
                                                   options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys
                                                     error:nil];
    // atomically:YES writes a temporary file and renames it over state.json.
    return data != nil && [data writeToFile:path atomically:YES];
}

static NSString *StringOrNil(id value) {
    return [value isKindOfClass:[NSString class]] && [(NSString *)value length] > 0 ? (NSString *)value : nil;
}

static NSString *HTTPGet(NSString *urlString, NSTimeInterval timeout) {
    NSURL *url = [NSURL URLWithString:urlString];
    if (!url) {
        return nil;
    }
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url];
    request.timeoutInterval = timeout;
    dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
    __block NSData *responseData = nil;
    __block NSHTTPURLResponse *httpResponse = nil;
    NSURLSessionConfiguration *config = [NSURLSessionConfiguration ephemeralSessionConfiguration];
    config.timeoutIntervalForRequest = timeout;
    config.timeoutIntervalForResource = timeout;
    NSURLSession *session = [NSURLSession sessionWithConfiguration:config];
    NSURLSessionDataTask *task = [session dataTaskWithRequest:request
                                            completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        if (!error && [response isKindOfClass:[NSHTTPURLResponse class]]) {
            httpResponse = (NSHTTPURLResponse *)response;
            responseData = data;
        }
        dispatch_semaphore_signal(semaphore);
    }];
    [task resume];
    if (dispatch_semaphore_wait(semaphore, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(timeout * NSEC_PER_SEC))) != 0) {
        [task cancel];
        [session invalidateAndCancel];
        return nil;
    }
    [session finishTasksAndInvalidate];
    if (!httpResponse || httpResponse.statusCode < 200 || httpResponse.statusCode >= 300) {
        return nil;
    }
    return responseData ? [[NSString alloc] initWithData:responseData encoding:NSUTF8StringEncoding] : @"";
}

@interface TrialHost : NSObject <NSApplicationDelegate>
@property(nonatomic, strong) NSStatusItem *statusItem;
@property(nonatomic, strong) NSTask *pythonTask;
@property(nonatomic, copy) NSString *seedDir;
@property(nonatomic, copy) NSString *seedVersion;
@property(nonatomic, copy) NSString *versionDir;
@property(nonatomic, copy) NSString *activeVersion;
@property(nonatomic, copy) NSArray<NSString *> *launchArguments;
@property(nonatomic, assign) NSInteger port;
@property(nonatomic, assign) NSInteger launches;
@property(nonatomic, assign) BOOL verified;
@property(nonatomic, strong) NSTimer *readyTimer;
@property(nonatomic, strong) NSDate *spawnedAt;
@end

@implementation TrialHost

- (instancetype)init {
    self = [super init];
    if (!self) {
        return nil;
    }
    NSBundle *bundle = [NSBundle mainBundle];
    self.seedDir = bundle.resourcePath ?: @"";
    self.seedVersion = [bundle objectForInfoDictionaryKey:@"CFBundleShortVersionString"] ?: @"0";
    self.launchArguments = [self configuredArguments];
    self.port = [self portFromArguments:self.launchArguments];
    EnsureDir(RowBotHomeDataDir());
    return self;
}

- (NSArray<NSString *> *)configuredArguments {
    NSString *path = [TrialRoot() stringByAppendingPathComponent:@"host-args.json"];
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (data) {
        id json = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
        if ([json isKindOfClass:[NSArray class]] && [(NSArray *)json count] > 0) {
            return (NSArray<NSString *> *)json;
        }
    }
    return @[@"launcher.py", @"--no-tray", @"--native"];
}

- (NSInteger)portFromArguments:(NSArray<NSString *> *)arguments {
    NSUInteger index = [arguments indexOfObject:@"--port"];
    if (index != NSNotFound && index + 1 < arguments.count) {
        return [arguments[index + 1] integerValue];
    }
    return 8080;
}

- (NSString *)pythonIn:(NSString *)dir {
    return [dir stringByAppendingPathComponent:@"python/bin/python3"];
}

// Resolve the version to start. When `apply` is set, also apply the
// rollback rule and persist it (a launch); probes only read.
- (void)resolveVersionApplyingRule:(BOOL)apply {
    NSFileManager *fm = [NSFileManager defaultManager];
    NSMutableDictionary *state = ReadState();
    NSString *versionsDir = [TrialRoot() stringByAppendingPathComponent:@"versions"];
    if (state == nil) {
        state = [@{@"schema": @1, @"active": self.seedVersion, @"previous": [NSNull null], @"ready": [NSNull null],
                   @"pending_verification": @NO, @"failed_starts": @0} mutableCopy];
        if (apply) {
            WriteState(state);
        }
        TrialLog(@"state_missing using_seed=%@", self.seedVersion);
    }
    if (apply && [[state objectForKey:@"pending_verification"] boolValue]) {
        NSInteger failed = [[state objectForKey:@"failed_starts"] integerValue];
        NSString *previous = StringOrNil([state objectForKey:@"previous"]);
        if (failed >= RowBotMaxFailedStarts && previous) {
            NSString *bad = StringOrNil([state objectForKey:@"active"]) ?: @"?";
            state[@"active"] = previous;
            state[@"previous"] = [NSNull null];
            state[@"pending_verification"] = @NO;
            state[@"failed_starts"] = @0;
            state[@"last_rollback"] = @{@"from": bad, @"to": previous, @"reason": @"2 failed starts",
                                        @"at": [[NSISO8601DateFormatter new] stringFromDate:[NSDate date]]};
            TrialLog(@"rollback from=%@ to=%@", bad, previous);
        } else {
            state[@"failed_starts"] = @(failed + 1);
        }
        WriteState(state);
    }
    NSString *active = StringOrNil([state objectForKey:@"active"]) ?: self.seedVersion;
    NSString *candidate = [versionsDir stringByAppendingPathComponent:active];
    BOOL staged = [fm isExecutableFileAtPath:[self pythonIn:candidate]];
    // Seed rule: a newer bundle (e.g. a dragged-in DMG) wins over older staged versions.
    if ([self.seedVersion compare:active options:NSNumericSearch] == NSOrderedDescending) {
        TrialLog(@"seed_newer seed=%@ active=%@", self.seedVersion, active);
        staged = NO;
        active = self.seedVersion;
        if (apply) {
            state[@"active"] = active;
            state[@"pending_verification"] = @NO;
            WriteState(state);
        }
    }
    if (staged) {
        self.versionDir = candidate;
    } else {
        self.versionDir = self.seedDir;
        if (![active isEqualToString:self.seedVersion]) {
            TrialLog(@"active_missing active=%@ using_seed=%@", active, self.seedVersion);
            active = self.seedVersion;
        }
    }
    self.activeVersion = active;
}

- (NSMutableDictionary *)environmentFor:(NSString *)dir {
    NSMutableDictionary *env = [[[NSProcessInfo processInfo] environment] mutableCopy];
    [env setObject:dir forKey:@"ROW_BOT_INSTALL_ROOT"];
    [env setObject:RowBotHomeDataDir() forKey:@"ROW_BOT_DATA_DIR"];
    [env setObject:@"1" forKey:@"PYTHONNOUSERSITE"];
    [env setObject:@"utf-8" forKey:@"PYTHONIOENCODING"];
    [env setObject:@"1" forKey:@"PYTHONDONTWRITEBYTECODE"];
    [env setObject:@"1" forKey:@"ROW_BOT_NATIVE_TRAY_HOST"];
    NSString *browsers = [dir stringByAppendingPathComponent:@"python/playwright-browsers"];
    BOOL isDir = NO;
    if ([[NSFileManager defaultManager] fileExistsAtPath:browsers isDirectory:&isDir] && isDir) {
        [env setObject:browsers forKey:@"PLAYWRIGHT_BROWSERS_PATH"];
    } else {
        NSString *userBrowsers = [RowBotHomeDataDir() stringByAppendingPathComponent:@"playwright-browsers"];
        EnsureDir(userBrowsers);
        [env setObject:userBrowsers forKey:@"PLAYWRIGHT_BROWSERS_PATH"];
    }
    return env;
}

- (NSTask *)runPython:(NSArray<NSString *> *)arguments inDir:(NSString *)dir wait:(BOOL)wait output:(NSString *)outputPath {
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:[self pythonIn:dir]];
    task.currentDirectoryURL = [NSURL fileURLWithPath:[dir stringByAppendingPathComponent:@"app"]];
    task.arguments = arguments;
    task.environment = [self environmentFor:dir];
    if (outputPath) {
        [[NSFileManager defaultManager] createFileAtPath:outputPath contents:nil attributes:nil];
        NSFileHandle *out = [NSFileHandle fileHandleForWritingAtPath:outputPath];
        task.standardOutput = out;
        task.standardError = out;
    }
    NSError *error = nil;
    if (![task launchAndReturnError:&error]) {
        TrialLog(@"python_launch_failed dir=%@ error=%@", dir, error.localizedDescription ?: @"unknown");
        return nil;
    }
    if (wait) {
        [task waitUntilExit];
    }
    return task;
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
    [self installStatusItem];
    TrialLog(@"host_started bundle=%@ seed=%@ args=%@", [[NSBundle mainBundle] bundlePath], self.seedVersion,
             [self.launchArguments componentsJoinedByString:@" "]);
    [self launchPrimary];
}

- (void)launchPrimary {
    if (self.launches >= RowBotMaxLaunches) {
        TrialLog(@"max_launches_reached");
        return;
    }
    self.launches += 1;
    [self resolveVersionApplyingRule:YES];
    NSString *logPath = [TrialRoot() stringByAppendingPathComponent:
                         [NSString stringWithFormat:@"launcher-%@-%ld.log", self.activeVersion, (long)self.launches]];
    self.verified = NO;
    self.spawnedAt = [NSDate date];
    NSTask *task = [self runPython:self.launchArguments inDir:self.versionDir wait:NO output:logPath];
    if (!task) {
        return;
    }
    self.pythonTask = task;
    NSString *version = self.activeVersion;
    TrialLog(@"spawned version=%@ dir=%@ pid=%d launch=%ld", version, self.versionDir, task.processIdentifier, (long)self.launches);
    __weak TrialHost *weakSelf = self;
    task.terminationHandler = ^(NSTask *finished) {
        dispatch_async(dispatch_get_main_queue(), ^{
            [weakSelf launcherExited:finished version:version];
        });
    };
    [self.readyTimer invalidate];
    self.readyTimer = [NSTimer scheduledTimerWithTimeInterval:0.25 target:self selector:@selector(pollReady:) userInfo:version repeats:YES];
}

- (void)pollReady:(NSTimer *)timer {
    NSString *version = timer.userInfo;
    if (self.verified || !self.pythonTask.running) {
        [timer invalidate];
        return;
    }
    if ([[NSDate date] timeIntervalSinceDate:self.spawnedAt] > 240) {
        TrialLog(@"ready_timeout version=%@", version);
        [timer invalidate];
        return;
    }
    NSString *url = [NSString stringWithFormat:@"http://127.0.0.1:%ld/readyz", (long)self.port];
    if (HTTPGet(url, 0.8) == nil) {
        return;
    }
    self.verified = YES;
    [timer invalidate];
    TrialLog(@"ready version=%@ seconds=%.2f", version, [[NSDate date] timeIntervalSinceDate:self.spawnedAt]);
    NSMutableDictionary *state = ReadState();
    if (state && [[state objectForKey:@"pending_verification"] boolValue] && [StringOrNil(state[@"active"]) isEqualToString:version]) {
        state[@"pending_verification"] = @NO;
        state[@"failed_starts"] = @0;
        state[@"verified_at"] = [[NSISO8601DateFormatter new] stringFromDate:[NSDate date]];
        WriteState(state);
        TrialLog(@"verified version=%@", version);
    }
}

- (void)launcherExited:(NSTask *)task version:(NSString *)version {
    TrialLog(@"launcher_exited version=%@ pid=%d status=%d verified=%d", version, task.processIdentifier,
             task.terminationStatus, self.verified);
    if (task != self.pythonTask) {
        return;
    }
    NSMutableDictionary *state = ReadState();
    if ([[state objectForKey:@"restart"] boolValue]) {
        [state removeObjectForKey:@"restart"];
        WriteState(state);
        TrialLog(@"restart_requested next=%@", state[@"active"]);
        [self launchPrimary];
        return;
    }
    if ([[state objectForKey:@"pending_verification"] boolValue] && !self.verified) {
        TrialLog(@"failed_start version=%@ failed_starts=%@", version, state[@"failed_starts"]);
        [self launchPrimary];
        return;
    }
    TrialLog(@"launcher_quit_no_restart");
}

// ------------------------------------------------------------------ menu / kit

- (void)installStatusItem {
    self.statusItem = [[NSStatusBar systemStatusBar] statusItemWithLength:NSVariableStatusItemLength];
    self.statusItem.button.title = @"RB*";
    self.statusItem.button.toolTip = @"Row-Bot (split-and-switch trial)";
    NSMenu *menu = [[NSMenu alloc] initWithTitle:RowBotAppName];
    NSArray *items = @[
        @[@"Open in Browser", @"openInBrowser:"],
        @[@"-", @""],
        @[@"Trial: Show status", @"showStatus:"],
        @[@"Trial: Microphone test (Python)", @"micTest:"],
        @[@"Trial: Camera test (Python)", @"cameraTest:"],
        @[@"Trial: Stage the update", @"stageUpdate:"],
        @[@"Trial: Restart to update", @"restartToUpdate:"],
        @[@"-", @""],
        @[@"Quit", @"quitRowBot:"],
    ];
    for (NSArray *item in items) {
        if ([item[0] isEqualToString:@"-"]) {
            [menu addItem:[NSMenuItem separatorItem]];
            continue;
        }
        NSMenuItem *menuItem = [[NSMenuItem alloc] initWithTitle:item[0] action:NSSelectorFromString(item[1]) keyEquivalent:@""];
        menuItem.target = self;
        [menu addItem:menuItem];
    }
    self.statusItem.menu = menu;
}

- (void)showText:(NSString *)title body:(NSString *)body {
    [NSApp activateIgnoringOtherApps:YES];
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = title;
    alert.informativeText = body.length > 4000 ? [body substringFromIndex:body.length - 4000] : (body ?: @"");
    [alert runModal];
}

- (void)runKit:(NSString *)title arguments:(NSArray<NSString *> *)arguments {
    NSString *script = [self.seedDir stringByAppendingPathComponent:@"trial/kit.py"];
    NSString *outPath = [TrialRoot() stringByAppendingPathComponent:@"kit-output.txt"];
    NSMutableArray *args = [@[script] mutableCopy];
    [args addObjectsFromArray:arguments];
    [args addObject:[NSString stringWithFormat:@"--host-pid=%d", getpid()]];
    [self resolveVersionApplyingRule:NO];
    NSString *dir = self.versionDir;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        NSTask *task = [self runPython:args inDir:dir wait:YES output:outPath];
        NSString *text = [NSString stringWithContentsOfFile:outPath encoding:NSUTF8StringEncoding error:nil] ?: @"(no output)";
        int status = task ? task.terminationStatus : -1;
        dispatch_async(dispatch_get_main_queue(), ^{
            [self showText:[NSString stringWithFormat:@"%@ (exit %d)", title, status] body:text];
        });
    });
}

- (void)showStatus:(id)sender { (void)sender; [self runKit:@"Status" arguments:@[@"status"]]; }
- (void)micTest:(id)sender { (void)sender; [self runKit:@"Microphone test" arguments:@[@"mic"]]; }
- (void)cameraTest:(id)sender { (void)sender; [self runKit:@"Camera test" arguments:@[@"camera"]]; }
- (void)stageUpdate:(id)sender { (void)sender; [self runKit:@"Stage the update" arguments:@[@"stage"]]; }

- (void)restartToUpdate:(id)sender {
    (void)sender;
    NSMutableDictionary *state = ReadState();
    NSDictionary *ready = [state objectForKey:@"ready"];
    if (![ready isKindOfClass:[NSDictionary class]]) {
        [self showText:@"Restart to update" body:@"Nothing is staged. Use \"Trial: Stage the update\" first."];
        return;
    }
    state[@"previous"] = state[@"active"];
    state[@"active"] = ready[@"version"];
    state[@"ready"] = [NSNull null];
    state[@"pending_verification"] = @YES;
    state[@"failed_starts"] = @0;
    state[@"restart"] = @YES;
    WriteState(state);
    TrialLog(@"switch_requested to=%@", state[@"active"]);
    if (self.pythonTask.running) {
        [self.pythonTask terminate];  // SIGTERM: the launcher stops its server and window gracefully
    } else {
        [self launcherExited:self.pythonTask version:self.activeVersion];
    }
}

- (void)openInBrowser:(id)sender {
    (void)sender;
    NSString *urlString = [NSString stringWithFormat:@"http://127.0.0.1:%ld", (long)self.port];
    [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:urlString]];
}

- (void)quitRowBot:(id)sender {
    (void)sender;
    TrialLog(@"quit_requested");
    [self.readyTimer invalidate];
    if (self.pythonTask && self.pythonTask.running) {
        self.pythonTask.terminationHandler = nil;
        [self.pythonTask terminate];
    }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2.0 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [[NSStatusBar systemStatusBar] removeStatusItem:self.statusItem];
        [NSApp terminate:nil];
    });
}

@end

static int TrialProbe(NSString *outPath) {
    TrialHost *host = [[TrialHost alloc] init];
    [host resolveVersionApplyingRule:NO];
    NSString *script = [host.seedDir stringByAppendingPathComponent:@"trial/probe.py"];
    TrialLog(@"probe version=%@ dir=%@ out=%@", host.activeVersion, host.versionDir, outPath);
    NSString *log = [outPath stringByAppendingString:@".log"];
    NSTask *task = [host runPython:@[script, outPath, [NSString stringWithFormat:@"%d", getpid()], host.activeVersion, host.versionDir]
                             inDir:host.versionDir wait:YES output:log];
    return task ? task.terminationStatus : 1;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        for (int index = 1; index < argc; index++) {
            if (strcmp(argv[index], "--trial-probe") == 0 && index + 1 < argc) {
                return TrialProbe([NSString stringWithUTF8String:argv[index + 1]]);
            }
            if (strcmp(argv[index], "--self-test") == 0) {
                TrialHost *host = [[TrialHost alloc] init];
                [host resolveVersionApplyingRule:NO];
                printf("trial-host-self-test seed=%s active=%s dir=%s\n", host.seedVersion.UTF8String,
                       host.activeVersion.UTF8String, host.versionDir.UTF8String);
                return 0;
            }
        }
        NSApplication *app = [NSApplication sharedApplication];
        TrialHost *delegate = [[TrialHost alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
