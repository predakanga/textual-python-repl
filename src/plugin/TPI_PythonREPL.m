/* *********************************************************************
 *
 * Python REPL plugin for Textual
 *
 * This class is a thin shim. It boots an embedded CPython interpreter
 * that lives inside this bundle, then forwards the handful of plugin
 * protocol callbacks it cares about to a delegate object implemented
 * in Python (via PyObjC). Everything interesting lives in the
 * textual_repl Python package found in Contents/Resources/lib.
 *
 *********************************************************************** */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#import <AppKit/AppKit.h>

#import "TPI_PythonREPL.h"
#import "TPI_PythonREPLPreferences.h"

NS_ASSUME_NONNULL_BEGIN

static id <TPIPythonREPLDelegate> _pythonDelegate = nil;
static volatile BOOL _interceptsServerInput = NO;

static void TPILogPythonError(NSString *context)
{
	/* Must be called with the GIL held and an exception set */
	PyObject *exception = PyErr_GetRaisedException();

	NSString *description = @"(unknown error)";

	if (exception) {
		PyObject *traceback = PyImport_ImportModule("traceback");

		PyObject *lines = (traceback) ? PyObject_CallMethod(traceback, "format_exception", "O", exception) : NULL;

		PyObject *joined = (lines) ? PyUnicode_Join(PyUnicode_FromString(""), lines) : PyObject_Str(exception);

		if (joined && PyUnicode_Check(joined)) {
			description = @(PyUnicode_AsUTF8(joined));
		}

		Py_XDECREF(joined);
		Py_XDECREF(lines);
		Py_XDECREF(traceback);
		Py_DECREF(exception);
	}

	PyErr_Clear();

	NSLog(@"[Python REPL] %@: %@", context, description);
}

@implementation TPI_PythonREPL

#pragma mark -
#pragma mark Bridge (called from Python)

+ (void)setPythonDelegate:(nullable id <TPIPythonREPLDelegate>)delegate
{
	@synchronized (self) {
		_pythonDelegate = delegate;
	}
}

+ (nullable id <TPIPythonREPLDelegate>)pythonDelegate
{
	@synchronized (self) {
		return _pythonDelegate;
	}
}

+ (void)setInterceptsServerInput:(BOOL)interceptsServerInput
{
	_interceptsServerInput = interceptsServerInput;
}

#pragma mark -
#pragma mark Interpreter

- (void)pluginLoadedIntoMemory
{
	static dispatch_once_t onceToken;

	/* Python cannot be reinitialized once finalized and we never
	 finalize it, so only ever boot the interpreter once. */
	dispatch_once(&onceToken, ^{
		[self startInterpreter];
	});
}

- (void)startInterpreter
{
	NSBundle *bundle = [NSBundle bundleForClass:[self class]];

	NSString *pythonHome = [bundle.resourcePath stringByAppendingPathComponent:@"python"];
	NSString *pythonExecutable = [pythonHome stringByAppendingPathComponent:@"bin/python3"];
	NSString *libraryPath = [bundle.resourcePath stringByAppendingPathComponent:@"lib"];

	if ([[NSFileManager defaultManager] fileExistsAtPath:pythonHome] == NO) {
		NSLog(@"[Python REPL] Bundled Python runtime is missing: %@", pythonHome);

		return;
	}

	PyStatus status;

	PyConfig config;
	PyConfig_InitIsolatedConfig(&config);

	/* We are a guest in somebody else's process: leave signals,
	 the C stdio, and the bundle's (signed) contents alone. */
	config.install_signal_handlers = 0;
	config.configure_c_stdio = 0;
	config.write_bytecode = 0;
	config.site_import = 1;
	config.user_site_directory = 0;

	char *argv[] = { "textual", NULL };

	status = PyConfig_SetBytesArgv(&config, 1, argv);
	if (PyStatus_Exception(status)) goto fail;

	status = PyConfig_SetBytesString(&config, &config.home, pythonHome.fileSystemRepresentation);
	if (PyStatus_Exception(status)) goto fail;

	status = PyConfig_SetBytesString(&config, &config.executable, pythonExecutable.fileSystemRepresentation);
	if (PyStatus_Exception(status)) goto fail;

	status = Py_InitializeFromConfig(&config);
	if (PyStatus_Exception(status)) goto fail;

	PyConfig_Clear(&config);

	PyObject *sysPath = PySys_GetObject("path"); // borrowed
	PyObject *libraryPathObject = PyUnicode_DecodeFSDefault(libraryPath.fileSystemRepresentation);

	PyList_Insert(sysPath, 0, libraryPathObject);

	Py_DECREF(libraryPathObject);

	PyObject *bootstrap = PyImport_ImportModule("textual_repl.plugin");

	if (bootstrap == NULL) {
		TPILogPythonError(@"Failed to import textual_repl.plugin");
	} else {
		PyObject *result = PyObject_CallMethod(bootstrap, "start", "s", bundle.bundlePath.UTF8String);

		if (result == NULL) {
			TPILogPythonError(@"textual_repl.plugin.start() failed");
		}

		Py_XDECREF(result);
		Py_DECREF(bootstrap);
	}

	/* Release the GIL so that the threads Python just spawned can run.
	 The thread state is intentionally leaked: we never finalize. */
	(void)PyEval_SaveThread();

	return;

fail:
	PyConfig_Clear(&config);

	NSLog(@"[Python REPL] Failed to initialize Python: %s (%s)",
		  (status.err_msg) ? status.err_msg : "unknown error",
		  (status.func) ? status.func : "?");
}

- (void)pluginWillBeUnloadedFromMemory
{
	id <TPIPythonREPLDelegate> delegate = [TPI_PythonREPL pythonDelegate];

	if (delegate == nil) {
		return;
	}

	@try {
		[delegate shutdown];
	} @catch (NSException *exception) {
		NSLog(@"[Python REPL] shutdown raised: %@", exception);
	}
}

#pragma mark -
#pragma mark Plugin Protocol

- (nullable IRCMessage *)interceptServerInput:(IRCMessage *)input for:(IRCClient *)client
{
	if (_interceptsServerInput == NO) {
		return input;
	}

	id <TPIPythonREPLDelegate> delegate = [TPI_PythonREPL pythonDelegate];

	if (delegate == nil) {
		return input;
	}

	@try {
		id result = [delegate interceptServerInput:input client:client];

		if (result == nil || result == [NSNull null]) {
			return nil;
		}

		return result;
	} @catch (NSException *exception) {
		NSLog(@"[Python REPL] interceptServerInput raised: %@", exception);

		return input;
	}
}

#pragma mark -
#pragma mark Preferences

- (NSString *)pluginPreferencesPaneMenuItemName
{
	return @"Python REPL";
}

- (NSView *)pluginPreferencesPaneView
{
	static TPI_PythonREPLPreferences *preferences = nil;

	static dispatch_once_t onceToken;

	/* Textual asks for this on its plugin queue; views belong on the main thread. */
	dispatch_once(&onceToken, ^{
		if ([NSThread isMainThread]) {
			preferences = [TPI_PythonREPLPreferences new];
		} else {
			dispatch_sync(dispatch_get_main_queue(), ^{
				preferences = [TPI_PythonREPLPreferences new];
			});
		}
	});

	return preferences.view;
}

- (NSArray<NSString *> *)subscribedUserInputCommands
{
	return @[@"py", @"pyrepl"];
}

- (void)userInputCommandInvokedOnClient:(IRCClient *)client commandString:(NSString *)commandString messageString:(NSString *)messageString
{
	id <TPIPythonREPLDelegate> delegate = [TPI_PythonREPL pythonDelegate];

	if (delegate == nil) {
		NSLog(@"[Python REPL] /%@ ignored: the interpreter is not running", commandString.lowercaseString);

		return;
	}

	@try {
		[delegate userCommand:commandString message:messageString client:client];
	} @catch (NSException *exception) {
		NSLog(@"[Python REPL] userCommand raised: %@", exception);
	}
}

@end

NS_ASSUME_NONNULL_END
