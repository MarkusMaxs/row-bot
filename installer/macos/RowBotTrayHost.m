// Row-Bot's macOS tray host (Row-Bot.app/Contents/MacOS/row-bot).
//
// The bundle's Contents/Resources/{app,python} is the "seed" version. Updates
// stage further versions in ~/Library/Application Support/Row-Bot/versions/<v>
// and name the active one in state.json there. The host:
//   1. reads state.json and applies the seed rule (the bundle wins when it is
//      newer than active, or when there is no valid state) and the loop guard;
//   2. starts <active>/python/bin/python3 <active>/app/launcher.py with
//      --no-tray --native (or the arguments given after "--");
//   3. starts it again only when it exits with 75, or after a failed start of a
//      version that is not verified yet. Two unverified launches in a row roll
//      back to previous; a start not ready after 240 s counts as failed.
// The same rules live in src/row_bot/install_layout.py, Row-Bot.exe and the
// Linux row-bot wrapper. Run from a download or a disk image, it first offers
// to move itself to Applications.
#import <Cocoa/Cocoa.h>
#import <Foundation/Foundation.h>
#import <dispatch/dispatch.h>
#import <dlfcn.h>
#import <libproc.h>
#import <signal.h>

static NSString * const RowBotBundleIdentifier = @"ai.row-bot.assistant";
static NSString * const RowBotAppName = @"Row-Bot";
static NSString * const RowBotLauncherStateFileName = @"launcher_state.json";
static NSString * const RowBotPingId = @"row-bot";
static NSString * const RowBotSkipMoveDefaultsKey = @"RowBotDoNotOfferMoveToApplications";
static NSInteger const RowBotDefaultPort = 8080;
static NSInteger const RowBotPortScanCount = 50;
static int const RowBotRelaunchExitCode = 75;
static NSInteger const RowBotMaxUnverifiedLaunches = 2;
static NSTimeInterval const RowBotReadyTimeout = 240.0;
static NSTimeInterval const RowBotQuickRelaunch = 30.0;
static NSInteger const RowBotMaxQuickRelaunches = 3;

static NSString *RowBotHomeDataDir(void) {
    NSString *envDataDir = [[[NSProcessInfo processInfo] environment] objectForKey:@"ROW_BOT_DATA_DIR"];
    if (envDataDir.length > 0) {
        return [envDataDir stringByExpandingTildeInPath];
    }
    return [NSHomeDirectory() stringByAppendingPathComponent:@".row-bot"];
}

// The install root R: state.json and staged versions.
static NSString *RowBotInstallRoot(void) {
    NSString *configured = [[[NSProcessInfo processInfo] environment] objectForKey:@"ROW_BOT_INSTALL_ROOT"];
    if (configured.length > 0) {
        return [configured stringByExpandingTildeInPath];
    }
    NSString *support = [NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, YES) firstObject];
    return [support stringByAppendingPathComponent:RowBotAppName];
}

static BOOL RowBotEnsureDirectory(NSString *path) {
    if (path.length == 0) {
        return NO;
    }
    NSError *error = nil;
    BOOL ok = [[NSFileManager defaultManager] createDirectoryAtPath:path
                                        withIntermediateDirectories:YES
                                                         attributes:nil
                                                              error:&error];
    return ok || error == nil;
}

static void RowBotLog(NSString *dataDir, NSString *format, ...) {
    if (dataDir.length == 0) {
        dataDir = RowBotHomeDataDir();
    }
    RowBotEnsureDirectory(dataDir);
    NSString *logPath = [dataDir stringByAppendingPathComponent:@"launcher-host.log"];

    va_list args;
    va_start(args, format);
    NSString *message = [[NSString alloc] initWithFormat:format arguments:args];
    va_end(args);

    NSDateFormatter *formatter = [[NSDateFormatter alloc] init];
    formatter.dateFormat = @"yyyy-MM-dd HH:mm:ss";
    NSString *line = [NSString stringWithFormat:@"%@ %@\n", [formatter stringFromDate:[NSDate date]], message];

    NSFileHandle *handle = [NSFileHandle fileHandleForWritingAtPath:logPath];
    if (!handle) {
        [line writeToFile:logPath atomically:YES encoding:NSUTF8StringEncoding error:nil];
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

static NSDictionary *RowBotReadJSONFile(NSString *path) {
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (!data) {
        return nil;
    }
    NSError *error = nil;
    id json = [NSJSONSerialization JSONObjectWithData:data options:NSJSONReadingMutableContainers error:&error];
    if (![json isKindOfClass:[NSDictionary class]]) {
        return nil;
    }
    return (NSDictionary *)json;
}

static NSInteger RowBotIntegerValue(id value) {
    if ([value respondsToSelector:@selector(integerValue)]) {
        return [value integerValue];
    }
    return 0;
}

// -- Versions and state.json (src/row_bot/install_layout.py) ----------------

// A version folder name: [0-9A-Za-z][0-9A-Za-z._+-]{0,63}, no "..".
static BOOL RowBotIsVersionName(id value) {
    if (![value isKindOfClass:[NSString class]]) {
        return NO;
    }
    NSString *name = (NSString *)value;
    if ([name containsString:@".."]) {
        return NO;
    }
    NSRegularExpression *pattern = [NSRegularExpression regularExpressionWithPattern:@"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"
                                                                             options:0
                                                                               error:nil];
    return [pattern numberOfMatchesInString:name options:0 range:NSMakeRange(0, name.length)] == 1;
}

static NSString *RowBotVersionOrNil(id value) {
    return RowBotIsVersionName(value) ? (NSString *)value : nil;
}

// 5.2.10 > 5.2.9, and a release beats its pre-releases (5.2.0 > 5.2.0-rc.1).
static NSComparisonResult RowBotCompareVersions(NSString *left, NSString *right) {
    NSString *(^release)(NSString *) = ^NSString *(NSString *value) {
        NSRange range = [value rangeOfString:@"^[0-9]+(\\.[0-9]+)*" options:NSRegularExpressionSearch];
        return range.location == NSNotFound ? @"" : [value substringWithRange:range];
    };
    NSString *leftRelease = release(left), *rightRelease = release(right);
    NSArray<NSString *> *leftParts = [leftRelease componentsSeparatedByString:@"."];
    NSArray<NSString *> *rightParts = [rightRelease componentsSeparatedByString:@"."];
    NSUInteger count = MAX(leftParts.count, rightParts.count);
    for (NSUInteger index = 0; index < count; index++) {
        NSInteger a = index < leftParts.count ? leftParts[index].integerValue : 0;
        NSInteger b = index < rightParts.count ? rightParts[index].integerValue : 0;
        if (a != b) {
            return a < b ? NSOrderedAscending : NSOrderedDescending;
        }
    }
    NSString *leftSuffix = [[left substringFromIndex:leftRelease.length] componentsSeparatedByString:@"+"].firstObject;
    NSString *rightSuffix = [[right substringFromIndex:rightRelease.length] componentsSeparatedByString:@"+"].firstObject;
    if ([leftSuffix isEqualToString:rightSuffix]) {
        return NSOrderedSame;
    }
    if (leftSuffix.length == 0 || rightSuffix.length == 0) {
        return leftSuffix.length == 0 ? NSOrderedDescending : NSOrderedAscending;
    }
    return [leftSuffix compare:rightSuffix options:NSNumericSearch];
}

static NSString *RowBotNowISO(void) {
    NSISO8601DateFormatter *formatter = [[NSISO8601DateFormatter alloc] init];
    return [formatter stringFromDate:[NSDate date]];
}

static NSString *RowBotStatePath(void) {
    return [RowBotInstallRoot() stringByAppendingPathComponent:@"state.json"];
}

static NSMutableDictionary *RowBotReadState(void) {
    NSMutableDictionary *state = (NSMutableDictionary *)RowBotReadJSONFile(RowBotStatePath());
    if (![state isKindOfClass:[NSMutableDictionary class]] || RowBotIntegerValue(state[@"schema"]) != 1 ||
        !RowBotIsVersionName(state[@"active"])) {
        return nil;
    }
    return state;
}

// atomically:YES writes a temporary file and renames it over state.json.
static BOOL RowBotWriteState(NSDictionary *state) {
    RowBotEnsureDirectory(RowBotInstallRoot());
    NSData *data = [NSJSONSerialization dataWithJSONObject:state
                                                   options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys
                                                     error:nil];
    return data != nil && [data writeToFile:RowBotStatePath() atomically:YES];
}

static NSMutableDictionary *RowBotNewState(NSString *active) {
    return [@{@"schema": @1, @"active": active, @"previous": [NSNull null], @"ready": [NSNull null],
              @"pending_verification": @NO, @"unverified_launches": @0, @"last_rollback": [NSNull null]} mutableCopy];
}

// Stops every process running from a version folder (a start that never got ready).
static void RowBotKillProcessesUnder(NSString *folder) {
    int count = proc_listallpids(NULL, 0);
    if (count <= 0) {
        return;
    }
    pid_t *pids = calloc((size_t)count + 16, sizeof(pid_t));
    count = proc_listallpids(pids, (int)(((size_t)count + 16) * sizeof(pid_t)));
    NSString *prefix = [folder stringByAppendingString:@"/"];
    for (int index = 0; index < count; index++) {
        char path[PROC_PIDPATHINFO_MAXSIZE];
        if (pids[index] > 0 && pids[index] != getpid() && proc_pidpath(pids[index], path, sizeof path) > 0 &&
            [[NSString stringWithUTF8String:path] hasPrefix:prefix]) {
            kill(pids[index], SIGKILL);
        }
    }
    free(pids);
}

static NSString *RowBotHTTP(NSString *urlString, NSString *method, NSTimeInterval timeout) {
    NSURL *url = [NSURL URLWithString:urlString];
    if (!url) {
        return nil;
    }

    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url];
    request.HTTPMethod = method ?: @"GET";
    request.timeoutInterval = timeout;
    if ([request.HTTPMethod isEqualToString:@"POST"]) {
        request.HTTPBody = [NSData data];
    }

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
    dispatch_time_t deadline = dispatch_time(DISPATCH_TIME_NOW, (int64_t)(timeout * NSEC_PER_SEC));
    if (dispatch_semaphore_wait(semaphore, deadline) != 0) {
        [task cancel];
        [session invalidateAndCancel];
        return nil;
    }
    [session finishTasksAndInvalidate];

    if (!httpResponse || httpResponse.statusCode < 200 || httpResponse.statusCode >= 300) {
        return nil;
    }
    if (!responseData) {
        return @"";
    }
    return [[NSString alloc] initWithData:responseData encoding:NSUTF8StringEncoding];
}

static BOOL RowBotPingPort(NSInteger port) {
    if (port <= 0) {
        return NO;
    }
    NSString *url = [NSString stringWithFormat:@"http://127.0.0.1:%ld/api/launcher-ping", (long)port];
    NSString *body = RowBotHTTP(url, @"GET", 0.6);
    if (body.length == 0) {
        return NO;
    }
    NSString *compact = [[[body lowercaseString] stringByReplacingOccurrencesOfString:@" " withString:@""]
                         stringByReplacingOccurrencesOfString:@"\n" withString:@""];
    return [compact containsString:[NSString stringWithFormat:@"\"app\":\"%@\"", RowBotPingId]];
}

// The folder a translocated app was opened from.
static NSString *RowBotOriginalBundlePath(NSString *path) {
    if (![path containsString:@"/AppTranslocation/"]) {
        return path;
    }
    void *security = dlopen("/System/Library/Frameworks/Security.framework/Security", RTLD_LAZY);
    CFURLRef (*originalForURL)(CFURLRef, CFErrorRef *) =
        security ? (CFURLRef (*)(CFURLRef, CFErrorRef *))dlsym(security, "SecTranslocateCreateOriginalPathForURL") : NULL;
    if (!originalForURL) {
        return path;
    }
    CFURLRef original = originalForURL((__bridge CFURLRef)[NSURL fileURLWithPath:path], NULL);
    return original ? [(__bridge_transfer NSURL *)original path] : path;
}

@interface RowBotHost : NSObject <NSApplicationDelegate>
@property(nonatomic, strong) NSStatusItem *statusItem;
@property(nonatomic, strong) NSTask *pythonTask;
@property(nonatomic, copy) NSString *seedDir;
@property(nonatomic, copy) NSString *seedVersion;
@property(nonatomic, copy) NSString *versionDir;
@property(nonatomic, copy) NSString *activeVersion;
@property(nonatomic, copy) NSArray<NSString *> *launchArguments;
@property(nonatomic, assign) BOOL argumentsGiven;
@property(nonatomic, copy) NSString *dataDir;
@property(nonatomic, copy) NSString *launcherStatePath;
@property(nonatomic, assign) NSInteger lastWindowPid;
@property(nonatomic, assign) BOOL pending;
@property(nonatomic, assign) BOOL timedOut;
@property(nonatomic, assign) BOOL quitting;
@property(nonatomic, assign) NSInteger quickRelaunches;
@property(nonatomic, strong) NSDate *launchedAt;
@property(nonatomic, strong) NSTimer *readyTimer;
@property(nonatomic, strong) dispatch_source_t termSource;
@end

@implementation RowBotHost

- (instancetype)init {
    self = [super init];
    if (!self) {
        return nil;
    }

    NSBundle *bundle = [NSBundle mainBundle];
    self.seedDir = bundle.resourcePath ?: @"";
    NSString *bundleVersion = [bundle objectForInfoDictionaryKey:@"CFBundleShortVersionString"];
    self.seedVersion = RowBotIsVersionName(bundleVersion) ? bundleVersion : @"0";
    self.versionDir = self.seedDir;
    self.activeVersion = self.seedVersion;
    self.dataDir = RowBotHomeDataDir();
    self.launcherStatePath = [self.dataDir stringByAppendingPathComponent:RowBotLauncherStateFileName];
    self.lastWindowPid = 0;

    // Arguments after "--" replace the default ones (smoke tests start a server this way).
    NSArray<NSString *> *argv = [[NSProcessInfo processInfo] arguments];
    NSUInteger separator = [argv indexOfObject:@"--"];
    self.argumentsGiven = separator != NSNotFound;
    self.launchArguments = self.argumentsGiven
        ? [argv subarrayWithRange:NSMakeRange(separator + 1, argv.count - separator - 1)]
        : @[@"--no-tray", @"--native"];
    RowBotEnsureDirectory(self.dataDir);
    return self;
}

- (NSString *)pythonIn:(NSString *)dir {
    return [dir stringByAppendingPathComponent:@"python/bin/python3"];
}

// Where a version's app/ and python/ are: the bundle for the seed version, else versions/<v>.
- (NSString *)folderForVersion:(NSString *)version {
    if (!RowBotIsVersionName(version)) {
        return nil;
    }
    if ([version isEqualToString:self.seedVersion]) {
        return self.seedDir;
    }
    NSString *folder = [[RowBotInstallRoot() stringByAppendingPathComponent:@"versions"] stringByAppendingPathComponent:version];
    NSFileManager *fm = [NSFileManager defaultManager];
    if ([fm isExecutableFileAtPath:[self pythonIn:folder]] &&
        [fm fileExistsAtPath:[folder stringByAppendingPathComponent:@"app/launcher.py"]]) {
        return folder;
    }
    return nil;
}

// Staged versions the bundle replaced are deleted in the background.
- (void)removeStagedVersionsUpTo:(NSString *)version {
    NSString *versions = [RowBotInstallRoot() stringByAppendingPathComponent:@"versions"];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_BACKGROUND, 0), ^{
        NSFileManager *fm = [NSFileManager defaultManager];
        for (NSString *name in [fm contentsOfDirectoryAtPath:versions error:nil] ?: @[]) {
            if (RowBotIsVersionName(name) && RowBotCompareVersions(name, version) != NSOrderedDescending) {
                [fm removeItemAtPath:[versions stringByAppendingPathComponent:name] error:nil];
                RowBotLog(nil, @"removed_staged_version version=%@", name);
            }
        }
    });
}

// The seed rule and the loop guard (install_layout.begin_launch), applied to every launch.
- (void)prepareLaunch {
    NSMutableDictionary *state = RowBotReadState();
    NSString *active = RowBotVersionOrNil(state[@"active"]);
    if (!state || RowBotCompareVersions(self.seedVersion, active) == NSOrderedDescending) {
        // No valid state, or a newer bundle (a dragged-in DMG): the bundle wins.
        RowBotLog(self.dataDir, @"seed_wins seed=%@ active=%@", self.seedVersion, active ?: @"none");
        state = RowBotNewState(self.seedVersion);
        RowBotWriteState(state);
        if (active) {
            [self removeStagedVersionsUpTo:self.seedVersion];
        }
        active = self.seedVersion;
    } else if (![self folderForVersion:active]) {
        RowBotLog(self.dataDir, @"active_missing active=%@ using_seed=%@", active, self.seedVersion);
        state = RowBotNewState(self.seedVersion);
        RowBotWriteState(state);
        active = self.seedVersion;
    }

    NSInteger launches = MAX(0, RowBotIntegerValue(state[@"unverified_launches"]));
    NSString *previous = RowBotVersionOrNil(state[@"previous"]);
    self.pending = NO;
    if (![state[@"pending_verification"] isEqual:@YES]) {
        if (launches > 0) {
            state[@"unverified_launches"] = @0;
            RowBotWriteState(state);
        }
    } else if (launches >= RowBotMaxUnverifiedLaunches && previous && ![previous isEqualToString:active] &&
               [self folderForVersion:previous]) {
        RowBotLog(self.dataDir, @"rollback from=%@ to=%@ launches=%ld", active, previous, (long)launches);
        NSDictionary *ready = state[@"ready"];
        if ([ready isKindOfClass:[NSDictionary class]] && [ready[@"version"] isEqual:active]) {
            state[@"ready"] = [NSNull null];
        }
        state[@"last_rollback"] = @{@"from": active, @"to": previous, @"at": RowBotNowISO(),
                                    @"reason": @"2 launches in a row did not become ready"};
        state[@"active"] = previous;
        state[@"previous"] = [NSNull null];
        state[@"pending_verification"] = @NO;
        state[@"unverified_launches"] = @0;
        RowBotWriteState(state);
        active = previous;
    } else {
        state[@"unverified_launches"] = @(launches + 1);
        RowBotWriteState(state);
        self.pending = YES;
    }
    self.activeVersion = active;
    self.versionDir = [self folderForVersion:active] ?: self.seedDir;
}

// After an exit (install_layout.relaunch_after_exit).
- (BOOL)relaunchAfterExit:(NSString *)version code:(int)code {
    if (code == RowBotRelaunchExitCode) {
        return YES;
    }
    NSDictionary *state = RowBotReadState();
    if (![state[@"pending_verification"] isEqual:@YES] || ![state[@"active"] isEqual:version] ||
        (!self.timedOut && code == 0)) {
        return NO;
    }
    return RowBotVersionOrNil(state[@"previous"]) != nil ||
           RowBotIntegerValue(state[@"unverified_launches"]) < RowBotMaxUnverifiedLaunches;
}

// SIGTERM stops the Python launcher (which stops its server and window) before the host exits.
- (void)handleTermination {
    signal(SIGTERM, SIG_IGN);
    self.termSource = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, SIGTERM, 0, dispatch_get_main_queue());
    __weak RowBotHost *weakSelf = self;
    dispatch_source_set_event_handler(self.termSource, ^{
        RowBotHost *host = weakSelf;
        RowBotLog(host.dataDir, @"sigterm");
        host.quitting = YES;
        [host.readyTimer invalidate];
        NSTask *task = host.pythonTask;
        if (task.running) {
            [task terminate];
        }
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            for (int waited = 0; task.running && waited < 120; waited++) {
                [NSThread sleepForTimeInterval:0.25];
            }
            exit(0);
        });
    });
    dispatch_resume(self.termSource);
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    [self handleTermination];
    [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
    if ([self offerMoveToApplications]) {
        return;
    }
    [self installStatusItem];
    RowBotLog(self.dataDir, @"host_started bundle=%@ seed=%@ root=%@",
              [[NSBundle mainBundle] bundlePath], self.seedVersion, RowBotInstallRoot());
    [self startPrimaryLauncherIfNeeded];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
    (void)sender;
    return NO;
}

// -- Move to Applications ---------------------------------------------------

- (BOOL)shouldOfferMoveToApplications {
    if (self.argumentsGiven || [[[NSProcessInfo processInfo] environment] objectForKey:@"ROW_BOT_NO_MOVE_TO_APPLICATIONS"] ||
        [[NSUserDefaults standardUserDefaults] boolForKey:RowBotSkipMoveDefaultsKey]) {
        return NO;
    }
    NSString *path = [[NSBundle mainBundle] bundlePath];
    if ([path containsString:@"/AppTranslocation/"]) {
        return YES;
    }
    NSString *downloads = [NSSearchPathForDirectoriesInDomains(NSDownloadsDirectory, NSUserDomainMask, YES) firstObject];
    if (downloads.length > 0 && [path hasPrefix:[downloads stringByAppendingString:@"/"]]) {
        return YES;
    }
    NSNumber *readOnly = nil;
    [[NSURL fileURLWithPath:path] getResourceValue:&readOnly forKey:NSURLVolumeIsReadOnlyKey error:nil];
    return readOnly.boolValue;
}

- (void)showError:(NSString *)message {
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = RowBotAppName;
    alert.informativeText = message;
    [alert runModal];
}

// Returns YES when Row-Bot was moved and started again from Applications.
- (BOOL)offerMoveToApplications {
    if (![self shouldOfferMoveToApplications]) {
        return NO;
    }
    [NSApp activateIgnoringOtherApps:YES];
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"Move Row-Bot to your Applications folder?";
    alert.informativeText = @"Row-Bot is running from a download or a disk image. From Applications it starts "
                            @"from the same place every time and can keep itself up to date.";
    [alert addButtonWithTitle:@"Move to Applications"];
    [alert addButtonWithTitle:@"Not Now"];
    alert.showsSuppressionButton = YES;
    alert.suppressionButton.title = @"Don't ask again";
    NSModalResponse response = [alert runModal];
    if (alert.suppressionButton.state == NSControlStateValueOn) {
        [[NSUserDefaults standardUserDefaults] setBool:YES forKey:RowBotSkipMoveDefaultsKey];
    }
    if (response != NSAlertFirstButtonReturn) {
        return NO;
    }

    NSFileManager *fm = [NSFileManager defaultManager];
    NSString *source = [[NSBundle mainBundle] bundlePath];
    NSString *original = RowBotOriginalBundlePath(source);
    NSString *applications = @"/Applications";
    if (![fm isWritableFileAtPath:applications]) {
        applications = [NSHomeDirectory() stringByAppendingPathComponent:@"Applications"];
        RowBotEnsureDirectory(applications);
    }
    NSString *destination = [applications stringByAppendingPathComponent:@"Row-Bot.app"];
    NSError *error = nil;
    if ([fm fileExistsAtPath:destination] &&
        ![fm trashItemAtURL:[NSURL fileURLWithPath:destination] resultingItemURL:nil error:&error]) {
        [self showError:[NSString stringWithFormat:@"Row-Bot could not replace %@: %@", destination,
                         error.localizedDescription ?: @"unknown error"]];
        return NO;
    }
    if (![fm copyItemAtPath:source toPath:destination error:&error]) {
        [self showError:[NSString stringWithFormat:@"Row-Bot could not copy itself to %@: %@", applications,
                         error.localizedDescription ?: @"unknown error"]];
        return NO;
    }
    // Moved by the user's choice: no new Gatekeeper check or translocation.
    NSTask *xattr = [NSTask launchedTaskWithExecutableURL:[NSURL fileURLWithPath:@"/usr/bin/xattr"]
                                                arguments:@[@"-d", @"-r", @"com.apple.quarantine", destination]
                                                    error:nil
                                       terminationHandler:nil];
    [xattr waitUntilExit];
    NSNumber *readOnly = nil;
    [[NSURL fileURLWithPath:original] getResourceValue:&readOnly forKey:NSURLVolumeIsReadOnlyKey error:nil];
    if (!readOnly.boolValue && ![original isEqualToString:destination]) {
        [fm trashItemAtURL:[NSURL fileURLWithPath:original] resultingItemURL:nil error:nil];
    }
    RowBotLog(self.dataDir, @"moved_to_applications from=%@ to=%@", original, destination);
    [NSTask launchedTaskWithExecutableURL:[NSURL fileURLWithPath:@"/usr/bin/open"]
                                arguments:@[@"-n", destination]
                                    error:nil
                       terminationHandler:nil];
    [NSApp terminate:nil];
    return YES;
}

// -- Tray -------------------------------------------------------------------

- (void)installStatusItem {
    self.statusItem = [[NSStatusBar systemStatusBar] statusItemWithLength:NSVariableStatusItemLength];
    self.statusItem.button.title = @"RB";
    self.statusItem.button.toolTip = RowBotAppName;
    self.statusItem.button.imagePosition = NSImageLeft;

    NSMenu *menu = [[NSMenu alloc] initWithTitle:RowBotAppName];
    NSMenuItem *openItem = [[NSMenuItem alloc] initWithTitle:@"Open Row-Bot"
                                                      action:@selector(openRowBot:)
                                               keyEquivalent:@""];
    openItem.target = self;
    [menu addItem:openItem];

    NSMenuItem *browserItem = [[NSMenuItem alloc] initWithTitle:@"Open in Browser"
                                                         action:@selector(openInBrowser:)
                                                  keyEquivalent:@""];
    browserItem.target = self;
    [menu addItem:browserItem];

    [menu addItem:[NSMenuItem separatorItem]];

    NSMenuItem *showBuddyItem = [[NSMenuItem alloc] initWithTitle:@"Show Buddy"
                                                           action:@selector(showBuddy:)
                                                    keyEquivalent:@""];
    showBuddyItem.target = self;
    [menu addItem:showBuddyItem];

    NSMenuItem *hideBuddyItem = [[NSMenuItem alloc] initWithTitle:@"Hide Buddy"
                                                           action:@selector(hideBuddy:)
                                                    keyEquivalent:@""];
    hideBuddyItem.target = self;
    [menu addItem:hideBuddyItem];

    [menu addItem:[NSMenuItem separatorItem]];

    NSMenuItem *quitItem = [[NSMenuItem alloc] initWithTitle:@"Quit"
                                                      action:@selector(quitRowBot:)
                                               keyEquivalent:@"q"];
    quitItem.target = self;
    [menu addItem:quitItem];

    self.statusItem.menu = menu;
}

// launcher_state.json in the data folder, written by the Python launcher.
- (NSDictionary *)launcherState {
    NSDictionary *state = RowBotReadJSONFile(self.launcherStatePath);
    if (![state isKindOfClass:[NSDictionary class]]) {
        return nil;
    }
    NSString *app = [state objectForKey:@"app"];
    if (app.length > 0 && ![[app lowercaseString] isEqualToString:RowBotPingId]) {
        return nil;
    }
    return state;
}

- (NSInteger)activePort {
    NSDictionary *state = [self launcherState];
    NSInteger statePort = RowBotIntegerValue([state objectForKey:@"port"]);
    if (statePort > 0 && RowBotPingPort(statePort)) {
        return statePort;
    }
    for (NSInteger port = RowBotDefaultPort; port < RowBotDefaultPort + RowBotPortScanCount; port++) {
        if (RowBotPingPort(port)) {
            return port;
        }
    }
    return 0;
}

- (NSInteger)activeControlPort {
    NSDictionary *state = [self launcherState];
    NSInteger port = RowBotIntegerValue([state objectForKey:@"port"]);
    if (port <= 0 || !RowBotPingPort(port)) {
        return 0;
    }
    return RowBotIntegerValue([state objectForKey:@"window_control_port"]);
}

- (NSInteger)activeWindowPid {
    NSDictionary *state = [self launcherState];
    NSInteger pid = RowBotIntegerValue([state objectForKey:@"window_pid"]);
    if (pid > 0) {
        self.lastWindowPid = pid;
    }
    return self.lastWindowPid;
}

// -- Starting Python --------------------------------------------------------

- (NSMutableDictionary *)launcherEnvironment {
    NSMutableDictionary *env = [[[NSProcessInfo processInfo] environment] mutableCopy];
    [env setObject:RowBotInstallRoot() forKey:@"ROW_BOT_INSTALL_ROOT"];
    [env setObject:@"1" forKey:@"PYTHONNOUSERSITE"];
    [env setObject:@"utf-8" forKey:@"PYTHONIOENCODING"];
    [env setObject:@"1" forKey:@"PYTHONDONTWRITEBYTECODE"];
    [env setObject:@"1" forKey:@"ROW_BOT_NATIVE_TRAY_HOST"];

    NSString *bundledBrowsers = [self.versionDir stringByAppendingPathComponent:@"python/playwright-browsers"];
    BOOL isDir = NO;
    if ([[NSFileManager defaultManager] fileExistsAtPath:bundledBrowsers isDirectory:&isDir] && isDir) {
        [env setObject:bundledBrowsers forKey:@"PLAYWRIGHT_BROWSERS_PATH"];
    } else {
        NSString *userBrowsers = [self.dataDir stringByAppendingPathComponent:@"playwright-browsers"];
        RowBotEnsureDirectory(userBrowsers);
        [env setObject:userBrowsers forKey:@"PLAYWRIGHT_BROWSERS_PATH"];
    }
    return env;
}

- (NSTask *)startPythonLauncherWithArguments:(NSArray<NSString *> *)arguments {
    NSString *python = [self pythonIn:self.versionDir];
    NSString *appDir = [self.versionDir stringByAppendingPathComponent:@"app"];
    NSString *launcher = [appDir stringByAppendingPathComponent:@"launcher.py"];
    if (![[NSFileManager defaultManager] isExecutableFileAtPath:python]) {
        RowBotLog(self.dataDir, @"missing_python path=%@", python);
        return nil;
    }
    if (![[NSFileManager defaultManager] fileExistsAtPath:launcher]) {
        RowBotLog(self.dataDir, @"missing_launcher path=%@", launcher);
        return nil;
    }

    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:python];
    task.currentDirectoryURL = [NSURL fileURLWithPath:appDir];
    task.arguments = [@[launcher] arrayByAddingObjectsFromArray:arguments];
    task.environment = [self launcherEnvironment];

    NSError *error = nil;
    if (![task launchAndReturnError:&error]) {
        RowBotLog(self.dataDir, @"python_launch_failed error=%@", error.localizedDescription ?: @"unknown");
        return nil;
    }
    RowBotLog(self.dataDir, @"python_launched version=%@ pid=%d args=%@", self.activeVersion,
              task.processIdentifier, [arguments componentsJoinedByString:@" "]);
    return task;
}

- (void)launchPrimary {
    [self prepareLaunch];
    self.timedOut = NO;
    self.launchedAt = [NSDate date];
    NSTask *task = [self startPythonLauncherWithArguments:self.launchArguments];
    if (!task) {
        return;
    }
    self.pythonTask = task;
    NSString *version = self.activeVersion;
    __weak RowBotHost *weakSelf = self;
    task.terminationHandler = ^(NSTask *finished) {
        dispatch_async(dispatch_get_main_queue(), ^{
            [weakSelf launcherExited:finished version:version];
        });
    };
    [self.readyTimer invalidate];
    self.readyTimer = nil;
    if (self.pending) {
        self.readyTimer = [NSTimer scheduledTimerWithTimeInterval:RowBotReadyTimeout
                                                           target:self
                                                         selector:@selector(readyTimeout:)
                                                         userInfo:version
                                                          repeats:NO];
    }
}

// A start of an unverified version that is not ready in time counts as failed.
- (void)readyTimeout:(NSTimer *)timer {
    NSString *version = timer.userInfo;
    NSDictionary *state = RowBotReadState();
    if (!self.pythonTask.running || ![state[@"pending_verification"] isEqual:@YES] ||
        ![state[@"active"] isEqual:version]) {
        return;
    }
    RowBotLog(self.dataDir, @"ready_timeout version=%@", version);
    self.timedOut = YES;
    NSTask *task = self.pythonTask;
    NSString *folder = [self.versionDir stringByAppendingPathComponent:@"python"];
    [task terminate];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(20.0 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        RowBotKillProcessesUnder(folder);
    });
}

- (void)launcherExited:(NSTask *)task version:(NSString *)version {
    int code = task.terminationReason == NSTaskTerminationReasonUncaughtSignal ? 128 + task.terminationStatus
                                                                                : task.terminationStatus;
    RowBotLog(self.dataDir, @"launcher_exited version=%@ pid=%d code=%d timed_out=%d", version,
              task.processIdentifier, code, self.timedOut);
    if (task != self.pythonTask || self.quitting) {
        return;
    }
    [self.readyTimer invalidate];
    self.readyTimer = nil;
    if (![self relaunchAfterExit:version code:code]) {
        return;
    }
    if (code == RowBotRelaunchExitCode && [[NSDate date] timeIntervalSinceDate:self.launchedAt] < RowBotQuickRelaunch) {
        if (++self.quickRelaunches >= RowBotMaxQuickRelaunches) {
            RowBotLog(self.dataDir, @"relaunch asked for %ld times within seconds; stopping", (long)self.quickRelaunches);
            return;
        }
    } else {
        self.quickRelaunches = 0;
    }
    RowBotLog(self.dataDir, @"relaunching");
    [self launchPrimary];
}

- (void)startPrimaryLauncherIfNeeded {
    NSInteger port = [self activePort];
    if (port > 0) {
        RowBotLog(self.dataDir, @"server_already_active port=%ld", (long)port);
        return;
    }
    if (self.pythonTask && self.pythonTask.running) {
        RowBotLog(self.dataDir, @"primary_launcher_already_running pid=%d", self.pythonTask.processIdentifier);
        return;
    }
    [self launchPrimary];
}

- (void)openRowBot:(id)sender {
    (void)sender;
    NSInteger port = [self activePort];
    if (port > 0) {
        [self startPythonLauncherWithArguments:@[
            @"--no-tray",
            @"--native",
            @"--no-splash",
            @"--no-ollama",
            @"--port",
            [NSString stringWithFormat:@"%ld", (long)port]
        ]];
        return;
    }
    [self startPrimaryLauncherIfNeeded];
}

- (void)openInBrowser:(id)sender {
    (void)sender;
    NSInteger port = [self activePort];
    if (port <= 0) {
        [self startPrimaryLauncherIfNeeded];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2.0 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            NSInteger retryPort = [self activePort];
            if (retryPort > 0) {
                NSString *retryURL = [NSString stringWithFormat:@"http://127.0.0.1:%ld", (long)retryPort];
                [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:retryURL]];
            } else {
                RowBotLog(self.dataDir, @"open_browser_retry_no_server");
            }
        });
        return;
    }
    NSString *urlString = [NSString stringWithFormat:@"http://127.0.0.1:%ld", (long)port];
    [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:urlString]];
}

- (BOOL)sendBuddyCommand:(NSString *)command {
    NSInteger controlPort = [self activeControlPort];
    if (controlPort <= 0) {
        return NO;
    }
    NSString *url = [NSString stringWithFormat:@"http://127.0.0.1:%ld/buddy/%@",
                     (long)controlPort, command ?: @""];
    NSString *body = RowBotHTTP(url, @"GET", 1.5);
    return body.length > 0 && [[body lowercaseString] containsString:@"true"];
}

- (void)showBuddy:(id)sender {
    (void)sender;
    if ([self sendBuddyCommand:@"show"]) {
        return;
    }
    [self openRowBot:nil];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2.0 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [self sendBuddyCommand:@"show"];
    });
}

- (void)hideBuddy:(id)sender {
    (void)sender;
    [self sendBuddyCommand:@"hide"];
}

- (void)terminateWindowIfKnown {
    NSInteger windowPid = [self activeWindowPid];
    if (windowPid > 0) {
        kill((pid_t)windowPid, SIGTERM);
        RowBotLog(self.dataDir, @"window_terminate_requested pid=%ld", (long)windowPid);
    }
}

- (void)quitRowBot:(id)sender {
    (void)sender;
    RowBotLog(self.dataDir, @"quit_requested");
    self.quitting = YES;
    [self.readyTimer invalidate];
    NSInteger port = [self activePort];
    if (port > 0) {
        NSString *url = [NSString stringWithFormat:@"http://127.0.0.1:%ld/api/launcher-shutdown", (long)port];
        RowBotHTTP(url, @"POST", 2.5);
    }
    [self terminateWindowIfKnown];
    [[NSFileManager defaultManager] removeItemAtPath:self.launcherStatePath error:nil];
    if (self.pythonTask && self.pythonTask.running) {
        [self.pythonTask terminate];
    }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2.0 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        if (self.pythonTask && self.pythonTask.running) {
            [self.pythonTask terminate];
        }
        [[NSStatusBar systemStatusBar] removeStatusItem:self.statusItem];
        [NSApp terminate:nil];
    });
}

@end

static int RowBotSelfTest(void) {
    RowBotHost *host = [[RowBotHost alloc] init];
    NSFileManager *fm = [NSFileManager defaultManager];
    BOOL ok = YES;
    NSMutableArray<NSString *> *failures = [NSMutableArray array];

    if (![[[NSBundle mainBundle] bundleIdentifier] isEqualToString:RowBotBundleIdentifier]) {
        ok = NO;
        [failures addObject:@"bundle identifier mismatch"];
    }
    if (![fm isExecutableFileAtPath:[host pythonIn:host.seedDir]]) {
        ok = NO;
        [failures addObject:@"bundled python missing"];
    }
    if (![fm fileExistsAtPath:[host.seedDir stringByAppendingPathComponent:@"app/launcher.py"]]) {
        ok = NO;
        [failures addObject:@"launcher.py missing"];
    }
    if ([host.seedVersion isEqualToString:@"0"]) {
        ok = NO;
        [failures addObject:@"bundle version missing"];
    }

    if (ok) {
        printf("row-bot-host-self-test ok bundle=%s seed=%s python=%s root=%s\n",
               [[[NSBundle mainBundle] bundlePath] UTF8String],
               [host.seedVersion UTF8String],
               [[host pythonIn:host.seedDir] UTF8String],
               [RowBotInstallRoot() UTF8String]);
        return 0;
    }

    fprintf(stderr, "row-bot-host-self-test failed: %s\n", [[failures componentsJoinedByString:@"; "] UTF8String]);
    return 1;
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        for (int index = 1; index < argc; index++) {
            if (strcmp(argv[index], "--") == 0) {
                break;
            }
            if (strcmp(argv[index], "--self-test") == 0) {
                return RowBotSelfTest();
            }
        }

        NSApplication *app = [NSApplication sharedApplication];
        RowBotHost *delegate = [[RowBotHost alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
