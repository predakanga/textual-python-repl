/* *********************************************************************
 *
 * Python REPL plugin for Textual
 *
 *********************************************************************** */

#import "TPI_PythonREPLPreferences.h"
#import "TPI_PythonREPL.h"

NS_ASSUME_NONNULL_BEGIN

/* Same size as the other addon panes, e.g. the Wiki-style link parser */
static const NSSize TPIPreferencesPaneSize = { 670.0, 470.0 };

@interface TPIPythonREPLPreferencesView : NSView
@property (nonatomic, copy, nullable) void (^windowChanged)(NSWindow * _Nullable window);
@end

@implementation TPIPythonREPLPreferencesView

- (void)viewDidMoveToWindow
{
	[super viewDidMoveToWindow];

	if (self.windowChanged) {
		self.windowChanged(self.window);
	}
}

@end

@interface TPI_PythonREPLPreferences ()
@property (readwrite) NSView *view;
@property (nonatomic, strong) NSButton *enabledCheckbox;
@property (nonatomic, strong) NSPopUpButton *hostPopup;
@property (nonatomic, strong) NSTextField *portField;
@property (nonatomic, strong) NSTextField *statusLabel;
@property (nonatomic, strong) NSTextField *commandLabel;
@property (nonatomic, strong) NSButton *commandCopyButton;
@property (nonatomic, strong) NSTextView *keysTextView;
@property (nonatomic, strong) NSPopUpButton *requestOutputPopup;
@property (nonatomic, strong) NSTextField *messageLabel;
@property (nonatomic, strong) NSButton *revertButton;
@property (nonatomic, strong) NSButton *applyButton;
@property (nonatomic, strong) NSButton *showFolderButton;
@property (nonatomic, strong, nullable) NSTimer *refreshTimer;
@property (nonatomic, copy, nullable) NSString *supportDirectory;
@property (nonatomic, assign) BOOL valuesLoaded;
@end

@implementation TPI_PythonREPLPreferences

- (instancetype)init
{
	if ((self = [super init])) {
		[self buildView];
	}

	return self;
}

#pragma mark -
#pragma mark Construction

static NSTextField *TPILabel(NSString *text)
{
	NSTextField *label = [NSTextField labelWithString:text];

	label.alignment = NSTextAlignmentRight;

	return label;
}

static NSTextField *TPIHelpLabel(NSString *text)
{
	NSTextField *label = [NSTextField wrappingLabelWithString:text];

	label.font = [NSFont systemFontOfSize:[NSFont smallSystemFontSize]];
	label.textColor = [NSColor secondaryLabelColor];

	return label;
}

- (void)buildView
{
	TPIPythonREPLPreferencesView *view = [[TPIPythonREPLPreferencesView alloc] initWithFrame:NSMakeRect(0, 0, TPIPreferencesPaneSize.width, TPIPreferencesPaneSize.height)];

	__weak typeof(self) weakSelf = self;

	view.windowChanged = ^(NSWindow *window) {
		[weakSelf viewMovedToWindow:window];
	};

	/* Textual's preferences window resizes itself to fit its content's
	 constraints. Without these the pane's fitting height is zero, and the
	 window shrinks to its minimum height, cutting the pane off. */
	[view.widthAnchor constraintEqualToConstant:TPIPreferencesPaneSize.width].active = YES;
	[view.heightAnchor constraintEqualToConstant:TPIPreferencesPaneSize.height].active = YES;

	/* SSH server */
	self.enabledCheckbox = [NSButton checkboxWithTitle:@"Run the SSH server" target:self action:@selector(controlChanged:)];

	self.hostPopup = [NSPopUpButton new];
	self.hostPopup.target = self;
	self.hostPopup.action = @selector(controlChanged:);

	NSNumberFormatter *portFormatter = [NSNumberFormatter new];
	portFormatter.numberStyle = NSNumberFormatterNoStyle;
	portFormatter.usesGroupingSeparator = NO;
	portFormatter.minimum = @1;
	portFormatter.maximum = @65535;
	portFormatter.allowsFloats = NO;

	self.portField = [NSTextField textFieldWithString:@""];
	self.portField.formatter = portFormatter;
	self.portField.placeholderString = @"2323";
	[self.portField.widthAnchor constraintEqualToConstant:64.0].active = YES;

	NSStackView *addressRow = [NSStackView stackViewWithViews:@[self.hostPopup, [NSTextField labelWithString:@"Port:"], self.portField]];
	addressRow.spacing = 8.0;

	self.statusLabel = [NSTextField wrappingLabelWithString:@"Starting…"];
	self.statusLabel.selectable = YES;

	self.commandLabel = [NSTextField labelWithString:@""];
	self.commandLabel.font = [NSFont monospacedSystemFontOfSize:[NSFont systemFontSize] weight:NSFontWeightRegular];
	self.commandLabel.selectable = YES;

	self.commandCopyButton = [NSButton buttonWithTitle:@"Copy" target:self action:@selector(copyCommand:)];
	self.commandCopyButton.controlSize = NSControlSizeSmall;
	self.commandCopyButton.font = [NSFont systemFontOfSize:[NSFont smallSystemFontSize]];

	NSStackView *commandRow = [NSStackView stackViewWithViews:@[self.commandLabel, self.commandCopyButton]];
	commandRow.spacing = 8.0;

	/* Authorized keys */
	NSScrollView *keysScrollView = [NSTextView scrollableTextView];
	keysScrollView.borderType = NSBezelBorder;
	keysScrollView.hasHorizontalScroller = NO;
	[keysScrollView.heightAnchor constraintEqualToConstant:110.0].active = YES;

	self.keysTextView = keysScrollView.documentView;
	self.keysTextView.richText = NO;
	self.keysTextView.font = [NSFont monospacedSystemFontOfSize:[NSFont smallSystemFontSize] weight:NSFontWeightRegular];
	self.keysTextView.automaticQuoteSubstitutionEnabled = NO;
	self.keysTextView.automaticDashSubstitutionEnabled = NO;
	self.keysTextView.automaticTextReplacementEnabled = NO;
	self.keysTextView.automaticSpellingCorrectionEnabled = NO;
	self.keysTextView.continuousSpellCheckingEnabled = NO;
	self.keysTextView.smartInsertDeleteEnabled = NO;

	NSStackView *keysColumn = [NSStackView stackViewWithViews:@[
		keysScrollView,
		TPIHelpLabel(@"One OpenSSH public key per line, such as the contents of ~/.ssh/id_ed25519.pub. Only these keys can connect.")
	]];
	keysColumn.orientation = NSUserInterfaceLayoutOrientationVertical;
	keysColumn.alignment = NSLayoutAttributeLeading;
	keysColumn.spacing = 4.0;
	[keysScrollView.widthAnchor constraintEqualToAnchor:keysColumn.widthAnchor].active = YES;

	/* Request replies */
	self.requestOutputPopup = [NSPopUpButton new];
	[self.requestOutputPopup addItemsWithTitles:@[@"In a REPL window for each session", @"Don't show them", @"In the active window (Textual's default)"]];
	self.requestOutputPopup.itemArray[0].representedObject = @"window";
	self.requestOutputPopup.itemArray[1].representedObject = @"hidden";
	self.requestOutputPopup.itemArray[2].representedObject = @"textual";
	self.requestOutputPopup.target = self;
	self.requestOutputPopup.action = @selector(controlChanged:);

	NSStackView *requestColumn = [NSStackView stackViewWithViews:@[
		self.requestOutputPopup,
		TPIHelpLabel(@"Where Textual shows the server's replies to requests made from Python, like await client.whois(nick).")
	]];
	requestColumn.orientation = NSUserInterfaceLayoutOrientationVertical;
	requestColumn.alignment = NSLayoutAttributeLeading;
	requestColumn.spacing = 4.0;

	/* Layout */
	NSGridView *grid = [NSGridView gridViewWithViews:@[
		@[[NSGridCell emptyContentView], self.enabledCheckbox],
		@[TPILabel(@"Listen on:"), addressRow],
		@[TPILabel(@"Status:"), self.statusLabel],
		@[TPILabel(@"Connect with:"), commandRow],
		@[TPILabel(@"Authorized keys:"), keysColumn],
		@[TPILabel(@"Request replies:"), requestColumn],
	]];
	grid.rowSpacing = 12.0;
	grid.columnSpacing = 8.0;
	[grid columnAtIndex:0].xPlacement = NSGridCellPlacementTrailing;
	[grid columnAtIndex:1].width = 440.0;
	grid.rowAlignment = NSGridRowAlignmentFirstBaseline;
	[grid rowAtIndex:4].yPlacement = NSGridCellPlacementTop;
	[grid rowAtIndex:4].rowAlignment = NSGridRowAlignmentNone;

	self.showFolderButton = [NSButton buttonWithTitle:@"Show Support Folder" target:self action:@selector(showSupportFolder:)];

	self.messageLabel = [NSTextField labelWithString:@""];
	self.messageLabel.lineBreakMode = NSLineBreakByTruncatingTail;
	[self.messageLabel setContentCompressionResistancePriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];

	self.revertButton = [NSButton buttonWithTitle:@"Revert" target:self action:@selector(revert:)];

	self.applyButton = [NSButton buttonWithTitle:@"Apply" target:self action:@selector(apply:)];
	self.applyButton.keyEquivalent = @"\r";

	NSStackView *buttonRow = [NSStackView stackViewWithViews:@[self.showFolderButton, self.messageLabel, self.revertButton, self.applyButton]];
	buttonRow.spacing = 8.0;

	/* Buttons keep their natural width; the message label takes the slack */
	for (NSView *button in @[self.showFolderButton, self.revertButton, self.applyButton]) {
		[button setContentHuggingPriority:NSLayoutPriorityRequired forOrientation:NSLayoutConstraintOrientationHorizontal];
	}

	[self.messageLabel setContentHuggingPriority:1.0 forOrientation:NSLayoutConstraintOrientationHorizontal];

	grid.translatesAutoresizingMaskIntoConstraints = NO;
	buttonRow.translatesAutoresizingMaskIntoConstraints = NO;

	[view addSubview:grid];
	[view addSubview:buttonRow];

	[NSLayoutConstraint activateConstraints:@[
		[grid.topAnchor constraintEqualToAnchor:view.topAnchor constant:24.0],
		[grid.centerXAnchor constraintEqualToAnchor:view.centerXAnchor],
		[buttonRow.leadingAnchor constraintEqualToAnchor:view.leadingAnchor constant:20.0],
		[buttonRow.trailingAnchor constraintEqualToAnchor:view.trailingAnchor constant:-20.0],
		[buttonRow.bottomAnchor constraintEqualToAnchor:view.bottomAnchor constant:-20.0],
	]];

	self.view = view;

	[self setControlsEnabled:NO];
}

#pragma mark -
#pragma mark Values

- (void)setControlsEnabled:(BOOL)enabled
{
	for (NSControl *control in @[self.enabledCheckbox, self.hostPopup, self.portField, self.requestOutputPopup, self.revertButton, self.applyButton, self.showFolderButton]) {
		control.enabled = enabled;
	}

	self.keysTextView.editable = enabled;

	[self updateEnabledState];
}

- (void)updateEnabledState
{
	BOOL serverControls = (self.valuesLoaded && self.enabledCheckbox.state == NSControlStateValueOn);

	self.hostPopup.enabled = serverControls;
	self.portField.enabled = serverControls;
}

- (void)selectHost:(NSString *)host
{
	[self.hostPopup removeAllItems];

	NSMutableArray *choices = [@[@[@"This Mac only (127.0.0.1)", @"127.0.0.1"], @[@"All network interfaces (0.0.0.0)", @"0.0.0.0"]] mutableCopy];

	if ([host isEqualToString:@"127.0.0.1"] == NO && [host isEqualToString:@"0.0.0.0"] == NO) {
		[choices addObject:@[host, host]]; // set by hand in config.json
	}

	for (NSArray *choice in choices) {
		[self.hostPopup addItemWithTitle:choice[0]];
		self.hostPopup.lastItem.representedObject = choice[1];

		if ([choice[1] isEqualToString:host]) {
			[self.hostPopup selectItem:self.hostPopup.lastItem];
		}
	}
}

- (void)loadValues
{
	id <TPIPythonREPLDelegate> delegate = [TPI_PythonREPL pythonDelegate];

	NSDictionary *preferences = nil;

	@try {
		preferences = [delegate preferences];
	} @catch (NSException *exception) {
		NSLog(@"[Python REPL] preferences raised: %@", exception);
	}

	if (preferences == nil) {
		self.valuesLoaded = NO;

		[self setControlsEnabled:NO];

		return;
	}

	self.enabledCheckbox.state = ([preferences[@"enabled"] boolValue]) ? NSControlStateValueOn : NSControlStateValueOff;

	[self selectHost:[preferences[@"host"] description]];

	self.portField.integerValue = [preferences[@"port"] integerValue];

	self.keysTextView.string = [preferences[@"authorized_keys"] description] ?: @"";

	NSString *requestOutput = [preferences[@"request_output"] description];

	for (NSMenuItem *item in self.requestOutputPopup.itemArray) {
		if ([item.representedObject isEqualToString:requestOutput]) {
			[self.requestOutputPopup selectItem:item];
		}
	}

	self.supportDirectory = [preferences[@"support_dir"] description];

	self.valuesLoaded = YES;

	[self setControlsEnabled:YES];

	[self showMessage:@"" isError:NO];
}

- (void)refreshStatus
{
	id <TPIPythonREPLDelegate> delegate = [TPI_PythonREPL pythonDelegate];

	if (delegate == nil) {
		self.statusLabel.stringValue = @"Starting…";

		return;
	}

	if (self.valuesLoaded == NO) {
		[self loadValues];
	}

	NSDictionary *status = nil;

	@try {
		status = [delegate status];
	} @catch (NSException *exception) {
		NSLog(@"[Python REPL] status raised: %@", exception);
	}

	if (status == nil) {
		return;
	}

	self.statusLabel.stringValue = [status[@"text"] description];
	self.statusLabel.textColor = ([status[@"ok"] boolValue]) ? [NSColor labelColor] : [NSColor systemRedColor];

	NSString *command = [status[@"command"] description];

	self.commandLabel.stringValue = (command.length > 0) ? command : @"—";
	self.commandCopyButton.enabled = (command.length > 0);
}

- (void)showMessage:(NSString *)message isError:(BOOL)isError
{
	self.messageLabel.stringValue = message;
	self.messageLabel.textColor = (isError) ? [NSColor systemRedColor] : [NSColor secondaryLabelColor];
	self.messageLabel.toolTip = (message.length > 0) ? message : nil;
}

#pragma mark -
#pragma mark Actions

- (void)controlChanged:(id)sender
{
	[self updateEnabledState];
}

- (void)apply:(id)sender
{
	id <TPIPythonREPLDelegate> delegate = [TPI_PythonREPL pythonDelegate];

	if (delegate == nil || self.valuesLoaded == NO) {
		return;
	}

	/* Commit an in-progress edit of the port field */
	[self.view.window makeFirstResponder:nil];

	NSDictionary *values = @{
		@"enabled" : @(self.enabledCheckbox.state == NSControlStateValueOn),
		@"host" : self.hostPopup.selectedItem.representedObject ?: @"127.0.0.1",
		@"port" : @(self.portField.integerValue),
		@"request_output" : self.requestOutputPopup.selectedItem.representedObject ?: @"window",
		@"authorized_keys" : self.keysTextView.string ?: @"",
	};

	id error = nil;

	@try {
		error = [delegate applyPreferences:values];
	} @catch (NSException *exception) {
		error = exception.reason ?: @"Something went wrong";
	}

	if (error != nil && error != [NSNull null]) {
		[self showMessage:[error description] isError:YES];

		NSBeep();

		return;
	}

	[self showMessage:@"Saved" isError:NO];

	[self refreshStatus];
}

- (void)revert:(id)sender
{
	[self loadValues];
}

- (void)copyCommand:(id)sender
{
	NSPasteboard *pasteboard = [NSPasteboard generalPasteboard];

	[pasteboard clearContents];

	[pasteboard setString:self.commandLabel.stringValue forType:NSPasteboardTypeString];
}

- (void)showSupportFolder:(id)sender
{
	if (self.supportDirectory == nil) {
		return;
	}

	[[NSWorkspace sharedWorkspace] activateFileViewerSelectingURLs:@[[NSURL fileURLWithPath:self.supportDirectory isDirectory:YES]]];
}

#pragma mark -
#pragma mark Visibility

- (void)viewMovedToWindow:(nullable NSWindow *)window
{
	[self.refreshTimer invalidate];

	self.refreshTimer = nil;

	if (window == nil) {
		return;
	}

	[self loadValues];

	[self refreshStatus];

	/* Status changes underneath us (sessions come and go, restarts finish) */
	__weak typeof(self) weakSelf = self;

	self.refreshTimer = [NSTimer timerWithTimeInterval:1.0 repeats:YES block:^(NSTimer *timer) {
		[weakSelf refreshStatus];
	}];

	[[NSRunLoop mainRunLoop] addTimer:self.refreshTimer forMode:NSRunLoopCommonModes];
}

@end

NS_ASSUME_NONNULL_END
