import AppKit
import ApplicationServices
import Foundation

// Inspect the focused Chrome window. Native clicks use current accessibility bounds.
func value(_ element: AXUIElement, _ key: String) -> CFTypeRef? {
    var result: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, key as CFString, &result) == .success ? result : nil
}
func string(_ element: AXUIElement, _ key: String) -> String {
    guard let item = value(element, key) else { return "" }
    if CFGetTypeID(item) == CFStringGetTypeID() { return item as! String }
    if CFGetTypeID(item) == CFURLGetTypeID() { return (item as! URL).absoluteString }
    return ""
}
func report(_ obj: [String: Any]) {
    FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: obj))
}
guard let chrome = NSRunningApplication.runningApplications(withBundleIdentifier:"com.google.Chrome").first else {
    report(["state":"Chrome closed"]); exit(0)
}
let app = AXUIElementCreateApplication(chrome.processIdentifier)
var front: CFTypeRef?
let error = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &front)
guard error == .success, let window = front else {
    report(["state":"Chrome accessibility unavailable","code":error.rawValue,"trusted":AXIsProcessTrusted()]); exit(0)
}
let args = CommandLine.arguments
if args.count == 4 && args[1] == "click" {
    guard NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "com.google.Chrome",
          let x = Double(args[2]), let y = Double(args[3]),
          CGDisplayBounds(CGMainDisplayID()).contains(CGPoint(x:x, y:y)) else { exit(4) }
    let point = CGPoint(x:x, y:y)
    for type: CGEventType in [.mouseMoved, .leftMouseDown, .leftMouseUp] {
        CGEvent(mouseEventSource:nil, mouseType:type, mouseCursorPosition:point,
                mouseButton:.left)?.post(tap:.cghidEventTap)
        usleep(35000)
    }
    exit(0)
}
var rows = [[String:Any]]()
func visit(_ el: AXUIElement, _ depth: Int) {
    guard depth < 48 && rows.count < 9000 else { return }
    let role=string(el,"AXRole"), title=string(el,"AXTitle"), desc=string(el,"AXDescription"), text=string(el,"AXValue"), url=string(el,"AXURL")
    let roles=["AXWebArea","AXButton","AXRadioButton","AXCheckBox","AXMenuItem","AXTextField","AXTextArea","AXLink","AXStaticText","AXSheet","AXDialog","AXWindow","AXRow","AXCell","AXPopUpButton","AXHeading","AXComboBox","AXList","AXImage","AXRadioGroup"]
    if roles.contains(role) {
        var pos=CGPoint.zero, size=CGSize.zero
        if let p=value(el,"AXPosition"),CFGetTypeID(p)==AXValueGetTypeID(){ AXValueGetValue(p as! AXValue,.cgPoint,&pos) }
        if let s=value(el,"AXSize"),CFGetTypeID(s)==AXValueGetTypeID(){ AXValueGetValue(s as! AXValue,.cgSize,&size) }
        rows.append(["role":role,"title":title,"desc":desc,"text":text,"url":url,
                     "x":pos.x,"y":pos.y,"w":size.width,"h":size.height,
                     "focused":value(el,"AXFocused") as? Bool ?? false,
                     "selected":(value(el,"AXSelected") as? Bool ?? false) || (value(el,"AXDOMClassList") as? [String] ?? []).contains("iron-selected"),
                     "checked":value(el,"AXValue") as? Int ?? -1,
                     "enabled":value(el,"AXEnabled") as? Bool ?? true])
    }
    if let children=value(el,"AXChildren") as? [AXUIElement] {
        for child in children { visit(child,depth+1) }
    }
}
visit(window as! AXUIElement,0)
report(["state":"ready","trusted":AXIsProcessTrusted(),"rows":rows])
