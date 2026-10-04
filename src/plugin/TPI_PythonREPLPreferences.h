/* *********************************************************************
 *
 * Python REPL plugin for Textual
 *
 *********************************************************************** */

#import <AppKit/AppKit.h>

NS_ASSUME_NONNULL_BEGIN

/* The pane shown under Preferences → Addons → Python REPL. Built in code
 (the plugin builds without Xcode, so no xib). Settings live in Python;
 this only talks to the Python delegate. Create on the main thread. */
@interface TPI_PythonREPLPreferences : NSObject
@property (readonly) NSView *view;
@end

NS_ASSUME_NONNULL_END
