/*
 * Test harness: loads PythonREPL.bundle the way THOPluginManager does and
 * provides just enough of Textual's object graph (as mocks) to exercise it.
 *
 *   Harness <bundle> <support dir>
 *
 * Lines on stdin drive it:
 *   <raw IRC line>     injected as server input on client 0 (via interceptServerInput)
 *   /py <code>         simulates typing /py in Textual
 *   /pyrepl            simulates typing /pyrepl
 *
 * Everything the "server" receives or Textual would print is logged to stdout.
 */

#import <AppKit/AppKit.h>

@protocol HarnessPlugin <NSObject>
- (void)pluginLoadedIntoMemory;
- (id)interceptServerInput:(id)input for:(id)client;
- (NSArray *)subscribedUserInputCommands;
- (void)userInputCommandInvokedOnClient:(id)client commandString:(NSString *)commandString messageString:(NSString *)messageString;
- (NSString *)pluginPreferencesPaneMenuItemName;
- (NSView *)pluginPreferencesPaneView;
@end

static NSString *supportDirectory = nil;
static id <HarnessPlugin> plugin = nil;
static dispatch_queue_t pluginQueue = NULL;

static void Out(NSString *format, ...) NS_FORMAT_FUNCTION(1, 2);
static void Out(NSString *format, ...)
{
	va_list args;
	va_start(args, format);
	NSString *line = [[NSString alloc] initWithFormat:format arguments:args];
	va_end(args);
	printf("%s\n", line.UTF8String);
	fflush(stdout);
}

static void AssertMain(const char *what)
{
	if ([NSThread isMainThread] == NO) {
		Out(@"!! %s called off the main thread", what);
	}
}

@class MockClient, MockChannel;

#pragma mark - Paths

@interface TPCPathInfo : NSObject
@end

@implementation TPCPathInfo
+ (NSString *)groupContainerApplicationSupport { return supportDirectory; }
@end

#pragma mark - Users

@interface MockUser : NSObject
@property (copy) NSString *nickname, *username, *address, *realName;
@property BOOL isAway, isIRCop;
@end

@implementation MockUser
@end

@interface MockMember : NSObject
@property (strong) MockUser *user;
@property (copy) NSString *modes, *mark;
@end

@implementation MockMember
@end

#pragma mark - Messages

@interface IRCMessage : NSObject
@property (copy) NSString *command, *senderNickname, *senderUsername, *senderAddress, *senderHostmask;
@property (copy) NSArray<NSString *> *params;
@property (copy) NSDictionary *messageTags;
@property (copy) NSDate *receivedAt;
@property BOOL isHistoric, senderIsServer;
- (instancetype)initWithLine:(NSString *)line onClient:(id)client;
@end

@implementation IRCMessage
- (instancetype)initWithLine:(NSString *)line onClient:(id)client
{
	if ((self = [super init]) == nil) return nil;
	NSString *rest = line;
	self.receivedAt = [NSDate date];
	self.senderIsServer = YES;
	if ([rest hasPrefix:@":"]) {
		NSRange space = [rest rangeOfString:@" "];
		if (space.location == NSNotFound) return nil;
		NSString *prefix = [rest substringWithRange:NSMakeRange(1, space.location - 1)];
		rest = [rest substringFromIndex:space.location + 1];
		NSRange bang = [prefix rangeOfString:@"!"];
		NSRange at = [prefix rangeOfString:@"@"];
		if (bang.location != NSNotFound && at.location != NSNotFound) {
			self.senderNickname = [prefix substringToIndex:bang.location];
			self.senderUsername = [prefix substringWithRange:NSMakeRange(bang.location + 1, at.location - bang.location - 1)];
			self.senderAddress = [prefix substringFromIndex:at.location + 1];
			self.senderIsServer = NO;
		} else {
			self.senderNickname = prefix;
		}
		self.senderHostmask = prefix;
	}
	NSMutableArray *params = [NSMutableArray array];
	while (rest.length > 0) {
		if ([rest hasPrefix:@":"]) { [params addObject:[rest substringFromIndex:1]]; break; }
		NSRange space = [rest rangeOfString:@" "];
		if (space.location == NSNotFound) { [params addObject:rest]; break; }
		[params addObject:[rest substringToIndex:space.location]];
		rest = [rest substringFromIndex:space.location + 1];
	}
	if (params.count == 0) return nil;
	self.command = params[0];
	[params removeObjectAtIndex:0];
	self.params = params;
	return self;
}
@end

#pragma mark - Channels

@interface MockChannel : NSObject
@property (copy) NSString *name, *topic, *uniqueIdentifier;
@property (weak) MockClient *associatedClient;
@property BOOL isPrivateMessage, isUtility;
@property NSUInteger status;
@property (strong) NSMutableArray<MockMember *> *members;
@end

@implementation MockChannel
- (BOOL)isClient { return NO; }
- (BOOL)isChannel { return self.isPrivateMessage == NO && self.isUtility == NO; }
- (id)memberInfo { AssertMain("memberInfo"); return self; }
- (NSArray *)memberList { return [self.members copy]; }
- (NSUInteger)numberOfMembers { return self.members.count; }
- (id)findMember:(NSString *)nick
{
	for (MockMember *m in self.members) {
		if ([m.user.nickname caseInsensitiveCompare:nick] == NSOrderedSame) return m;
	}
	return nil;
}
@end

#pragma mark - Clients

@interface MockConfig : NSObject
@property (copy) NSString *connectionName;
@end

@implementation MockConfig
@end

@interface MockClient : NSObject
@property (copy) NSString *uniqueIdentifier, *networkName, *serverAddress, *userNickname;
@property (strong) MockConfig *config;
@property (strong) NSMutableArray<MockChannel *> *channels;
@property BOOL isConnected, isLoggedIn, userIsAway;
@end

@implementation MockClient
- (BOOL)isClient { return YES; }
- (NSArray *)channelList { AssertMain("channelList"); return [self.channels copy]; }
- (id)findChannel:(NSString *)name
{
	for (MockChannel *c in self.channels) {
		if ([c.name caseInsensitiveCompare:name] == NSOrderedSame) return c;
	}
	return nil;
}
- (id)findUser:(NSString *)nick
{
	for (MockChannel *c in self.channels) {
		MockMember *m = [c findMember:nick];
		if (m) return m.user;
	}
	return nil;
}
- (NSArray *)userList
{
	NSMutableArray *users = [NSMutableArray array];
	for (MockChannel *c in self.channels) for (MockMember *m in c.members) [users addObject:m.user];
	return users;
}
- (BOOL)stringIsChannelName:(NSString *)string { return [string hasPrefix:@"#"]; }

- (id)findChannelOrCreate:(NSString *)name asType:(NSUInteger)type
{
	AssertMain("findChannelOrCreate:asType:");
	MockChannel *channel = [self findChannel:name];
	if (channel) return channel;
	channel = [MockChannel new];
	channel.name = name;
	channel.uniqueIdentifier = [NSUUID UUID].UUIDString;
	channel.associatedClient = self;
	channel.isUtility = (type == 2);
	channel.members = [NSMutableArray array];
	[self.channels addObject:channel];
	Out(@"-- created window %@ (type %lu)", name, (unsigned long)type);
	return channel;
}

- (void)inject:(NSString *)line
{
	IRCMessage *message = [[IRCMessage alloc] initWithLine:line onClient:self];
	/* Textual calls this on a (serial) connection queue, not the main thread */
	static dispatch_queue_t connectionQueue = NULL;
	static dispatch_once_t onceToken;
	dispatch_once(&onceToken, ^{
		connectionQueue = dispatch_queue_create("Harness.ConnectionQueue", DISPATCH_QUEUE_SERIAL);
	});
	dispatch_async(connectionQueue, ^{
		IRCMessage *result = [plugin interceptServerInput:message for:self];
		if (result == nil) {
			Out(@"<< DROPPED %@", line);
		} else if (result != message) {
			Out(@"<< REWRITTEN %@ %@ %@", result.command, result.senderNickname, [result.params componentsJoinedByString:@" "]);
		}
	});
}

- (void)sendLine:(NSString *)line
{
	AssertMain("sendLine:");
	Out(@">> %@", line);

	/* A tiny fake server */
	NSArray *words = [line componentsSeparatedByString:@" "];
	NSString *command = [words.firstObject uppercaseString];
	if ([command isEqualToString:@"PING"]) {
		[self inject:[NSString stringWithFormat:@":irc.example.net PONG irc.example.net %@", [line substringFromIndex:5]]];
	} else if ([command isEqualToString:@"WHOIS"] && words.count > 1) {
		NSString *nick = words[1];
		if ([self findUser:nick] == nil) {
			[self inject:[NSString stringWithFormat:@":irc.example.net 401 %@ %@ :No such nick/channel", self.userNickname, nick]];
			[self inject:[NSString stringWithFormat:@":irc.example.net 318 %@ %@ :End of /WHOIS list.", self.userNickname, nick]];
		} else {
			[self inject:[NSString stringWithFormat:@":irc.example.net 311 %@ %@ user example.org * :Real Name", self.userNickname, nick]];
			[self inject:[NSString stringWithFormat:@":irc.example.net 319 %@ %@ :@#python #textual", self.userNickname, nick]];
			[self inject:[NSString stringWithFormat:@":irc.example.net 312 %@ %@ irc.example.net :Example server", self.userNickname, nick]];
			[self inject:[NSString stringWithFormat:@":irc.example.net 330 %@ %@ account :is logged in as", self.userNickname, nick]];
			[self inject:[NSString stringWithFormat:@":irc.example.net 318 %@ %@ :End of /WHOIS list.", self.userNickname, nick]];
		}
	}
}

- (void)sendCommand:(id)string completeTarget:(BOOL)completeTarget target:(NSString *)target
{
	AssertMain("sendCommand:");
	Out(@">> [command] %@ (target=%@)", string, target);
}
- (void)sendPrivmsg:(NSString *)text toChannel:(MockChannel *)channel { AssertMain("sendPrivmsg"); Out(@">> PRIVMSG %@ :%@", channel.name, text); }
- (void)sendNotice:(NSString *)text toChannel:(MockChannel *)channel { AssertMain("sendNotice"); Out(@">> NOTICE %@ :%@", channel.name, text); }
- (void)sendAction:(NSString *)text toChannel:(MockChannel *)channel { AssertMain("sendAction"); Out(@">> PRIVMSG %@ :\1ACTION %@\1", channel.name, text); }
- (void)sendCTCPQuery:(NSString *)nick command:(NSString *)command text:(NSString *)text { Out(@">> CTCP %@ %@ %@", nick, command, text); }
- (void)joinUnlistedChannel:(NSString *)channel password:(NSString *)password { AssertMain("join"); Out(@">> JOIN %@", channel); }
- (void)partUnlistedChannel:(NSString *)channel withComment:(NSString *)comment { AssertMain("part"); Out(@">> PART %@ :%@", channel, comment); }
- (void)changeNickname:(NSString *)nick { Out(@">> NICK %@", nick); }
- (void)connect { Out(@">> [connect]"); }
- (void)quit { Out(@">> QUIT"); }
- (void)quitWithComment:(NSString *)comment { Out(@">> QUIT :%@", comment); }
- (void)printDebugInformationToConsole:(NSString *)text { AssertMain("print"); Out(@"[console] %@", text); }
- (void)printDebugInformation:(NSString *)text inChannel:(MockChannel *)channel { AssertMain("print"); Out(@"[%@] %@", channel.name, text); }
@end

#pragma mark - World

@interface MockWorld : NSObject
@property (strong) NSArray<MockClient *> *clientList;
@end

@implementation MockWorld
- (NSUInteger)clientCount { return self.clientList.count; }
@end

@interface MockMainWindow : NSObject
@property (weak) MockClient *selectedClient;
@property (weak) MockChannel *selectedChannel;
@end

@implementation MockMainWindow
- (id)selectedItem { return self.selectedChannel ?: (id)self.selectedClient; }
@end

@interface MockMaster : NSObject
@property (strong) MockWorld *world;
@property (strong) MockMainWindow *mainWindow;
@end

@implementation MockMaster
@end

static MockMaster *sharedMaster = nil;

@implementation NSObject (TXSharedApplicationObjectExtension)
+ (id)masterController { return sharedMaster; }
- (id)masterController { return sharedMaster; }
@end

#pragma mark - Fixture

static MockMember *Member(NSString *nick, NSString *modes, NSString *mark)
{
	MockUser *user = [MockUser new];
	user.nickname = nick;
	user.username = [nick lowercaseString];
	user.address = @"example.org";
	user.realName = [NSString stringWithFormat:@"%@ Example", nick];
	MockMember *member = [MockMember new];
	member.user = user;
	member.modes = modes;
	member.mark = mark;
	return member;
}

static MockClient *Client(NSString *identifier, NSString *name, NSString *network, NSString *nick)
{
	MockClient *client = [MockClient new];
	client.uniqueIdentifier = identifier;
	client.config = [MockConfig new];
	client.config.connectionName = name;
	client.networkName = network;
	client.serverAddress = [NSString stringWithFormat:@"irc.%@.net", network.lowercaseString];
	client.userNickname = nick;
	client.isConnected = YES;
	client.isLoggedIn = YES;
	client.channels = [NSMutableArray array];
	return client;
}

static MockChannel *Channel(MockClient *client, NSString *name, NSArray *members)
{
	MockChannel *channel = [MockChannel new];
	channel.name = name;
	channel.uniqueIdentifier = [NSUUID UUID].UUIDString;
	channel.associatedClient = client;
	channel.isPrivateMessage = ([name hasPrefix:@"#"] == NO);
	channel.status = 2;
	channel.topic = [NSString stringWithFormat:@"Welcome to %@", name];
	channel.members = [members mutableCopy];
	[client.channels addObject:channel];
	return channel;
}

int main(int argc, const char *argv[])
{
	@autoreleasepool {
		[NSApplication sharedApplication];

		if (argc < 3) {
			fprintf(stderr, "usage: %s <bundle> <support dir>\n", argv[0]);
			return 2;
		}

		supportDirectory = @(argv[2]);

		/* When sandboxed, use the container and seed it from the environment
		 (writing into a container from outside can trigger privacy prompts). */
		if ([supportDirectory isEqualToString:@"-"]) {
			supportDirectory = [NSHomeDirectory() stringByAppendingPathComponent:@"support"];
			NSString *replDirectory = [supportDirectory stringByAppendingPathComponent:@"Python REPL"];
			[[NSFileManager defaultManager] createDirectoryAtPath:replDirectory withIntermediateDirectories:YES attributes:nil error:NULL];
			NSDictionary *environment = [NSProcessInfo processInfo].environment;
			[environment[@"HARNESS_AUTHORIZED_KEY"] writeToFile:[replDirectory stringByAppendingPathComponent:@"authorized_keys"] atomically:YES encoding:NSUTF8StringEncoding error:NULL];
			[environment[@"HARNESS_CONFIG"] writeToFile:[replDirectory stringByAppendingPathComponent:@"config.json"] atomically:YES encoding:NSUTF8StringEncoding error:NULL];
			Out(@"-- sandboxed support directory: %@", supportDirectory);
		}

		MockClient *libera = Client(@"client-1", @"Libera", @"Libera.Chat", @"tester");
		MockChannel *python = Channel(libera, @"#python", @[Member(@"alice", @"o", @"@"), Member(@"bob", @"v", @"+"), Member(@"tester", @"", @"")]);
		Channel(libera, @"#textual", @[Member(@"carol", @"", @""), Member(@"tester", @"", @"")]);
		Channel(libera, @"alice", @[]);
		MockClient *oftc = Client(@"client-2", @"OFTC", @"OFTC", @"tester2");
		oftc.isConnected = NO;

		sharedMaster = [MockMaster new];
		sharedMaster.world = [MockWorld new];
		sharedMaster.world.clientList = @[libera, oftc];
		sharedMaster.mainWindow = [MockMainWindow new];
		sharedMaster.mainWindow.selectedClient = libera;
		sharedMaster.mainWindow.selectedChannel = python;

		pluginQueue = dispatch_queue_create("Harness.PluginQueue", DISPATCH_QUEUE_SERIAL);

		/* Mirror THOPluginManager / THOPluginItem */
		dispatch_async(pluginQueue, ^{
			NSBundle *bundle = [NSBundle bundleWithPath:@(argv[1])];
			if (bundle == nil || bundle.infoDictionary[@"MinimumTextualVersion"] == nil) {
				Out(@"!! bad bundle");
				exit(1);
			}
			Class principalClass = bundle.principalClass;
			if (principalClass == nil) {
				Out(@"!! no principal class");
				exit(1);
			}
			plugin = [principalClass new];
			if ([plugin respondsToSelector:@selector(pluginLoadedIntoMemory)]) {
				[plugin pluginLoadedIntoMemory];
			}
			Out(@"-- loaded; intercepts=%d commands=%@",
				[plugin respondsToSelector:@selector(interceptServerInput:for:)],
				[plugin subscribedUserInputCommands]);
		});

		/* stdin drives the harness */
		dispatch_async(dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0), ^{
			/* read(2) rather than stdio: fgets() would hold stdin's FILE lock,
			 which Python briefly needs during initialization. */
			NSMutableData *pending = [NSMutableData data];
			char chunk[8192];
			ssize_t count;
			while ((count = read(STDIN_FILENO, chunk, sizeof(chunk))) > 0) {
				[pending appendBytes:chunk length:count];
				while (YES) {
				NSRange newline = [pending rangeOfData:[NSData dataWithBytes:"\n" length:1] options:0 range:NSMakeRange(0, pending.length)];
				if (newline.location == NSNotFound) break;
				NSString *line = [[NSString alloc] initWithData:[pending subdataWithRange:NSMakeRange(0, newline.location)] encoding:NSUTF8StringEncoding];
				[pending replaceBytesInRange:NSMakeRange(0, newline.location + 1) withBytes:NULL length:0];
				if (line.length == 0) continue;
				if ([line hasPrefix:@"!snapshot "]) {
					NSString *path = [line substringFromIndex:10];
					NSString *appearanceName = [path containsString:@"dark"] ? NSAppearanceNameDarkAqua : NSAppearanceNameAqua;
					dispatch_async(dispatch_get_main_queue(), ^{
						NSView *pane = [plugin pluginPreferencesPaneView];
						NSWindow *window = [[NSWindow alloc] initWithContentRect:pane.frame styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
						window.appearance = [NSAppearance appearanceNamed:appearanceName];
						window.contentView = pane;
						dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.5 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
							[pane layoutSubtreeIfNeeded];
							/* Render through the frame view so the window background is included */
							NSView *frameView = pane.superview;
							NSRect rect = [pane convertRect:pane.bounds toView:frameView];
							NSBitmapImageRep *rep = [frameView bitmapImageRepForCachingDisplayInRect:rect];
							[frameView cacheDisplayInRect:rect toBitmapImageRep:rep];
							[[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:path atomically:YES];
							Out(@"-- snapshot %@ (%@, menu item '%@')", path, NSStringFromSize(pane.frame.size), [plugin pluginPreferencesPaneMenuItemName]);
							window.contentView = [NSView new]; // detach, as Textual does when switching panes
						});
					});
				} else if ([line hasPrefix:@"/"]) {
					NSRange space = [line rangeOfString:@" "];
					NSString *command = (space.location == NSNotFound) ? [line substringFromIndex:1] : [line substringWithRange:NSMakeRange(1, space.location - 1)];
					NSString *message = (space.location == NSNotFound) ? @"" : [line substringFromIndex:space.location + 1];
					dispatch_async(pluginQueue, ^{
						[plugin userInputCommandInvokedOnClient:(id)libera commandString:command.uppercaseString messageString:message];
					});
				} else {
					[libera inject:line];
				}
				}
			}
			Out(@"-- stdin closed");
		});

		[[NSRunLoop mainRunLoop] run];
	}
	return 0;
}
