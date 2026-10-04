/* *********************************************************************
 *
 * Python REPL plugin for Textual
 *
 *********************************************************************** */

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

@class IRCClient, IRCMessage;

/* Implemented in Python by textual_repl.plugin.PluginDelegate. Every method
 returns an object (PyObjC's default signature); void-ish ones return nil. */
@protocol TPIPythonREPLDelegate <NSObject>
- (nullable id)interceptServerInput:(IRCMessage *)input client:(IRCClient *)client;
- (nullable id)userCommand:(NSString *)command message:(NSString *)message client:(IRCClient *)client;
- (nullable id)shutdown;

/* Preferences pane. -preferences and -status return dictionaries;
 -applyPreferences: returns an error message, or nil on success. */
- (nullable id)preferences;
- (nullable id)applyPreferences:(NSDictionary *)preferences;
- (nullable id)status;
@end

@interface TPI_PythonREPL : NSObject
+ (nullable id <TPIPythonREPLDelegate>)pythonDelegate;

/* Called from Python */
+ (void)setPythonDelegate:(nullable id <TPIPythonREPLDelegate>)delegate;
+ (void)setInterceptsServerInput:(BOOL)interceptsServerInput;
@end

NS_ASSUME_NONNULL_END
