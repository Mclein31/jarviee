// Apple Calendar events as JSON: calendar_events <days_back> <days_ahead> [out_file]. Needs Calendar permission.
// With out_file (how the .app is run via `open`), the result goes there; an error is written as {"error":"denied"}.
// calendar_events apply <spec.json> <out_file>: create calendars / repeating events and change how long series repeat.
// Reads and writes live in this one app on purpose: every new or rebuilt app is a new identity to macOS and asks for
// Calendar access again, so keep this file stable (apply takes data, not code).
import EventKit
import Foundation
import CoreGraphics

let args = CommandLine.arguments
let back = args.count > 1 ? Int(args[1]) ?? 0 : 0
let ahead = args.count > 2 ? Int(args[2]) ?? 8 : 8
let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
if #available(macOS 14.0, *) {
    store.requestFullAccessToEvents { ok, _ in granted = ok; sem.signal() }
} else {
    store.requestAccess(to: .event) { ok, _ in granted = ok; sem.signal() }
}
_ = sem.wait(timeout: .now() + 60)
if args.count > 1 && args[1] == "apply" {
    applySpec(store, granted, args.count > 2 ? args[2] : "", args.count > 3 ? args[3] : "/dev/stdout")
}
let outFile = args.count > 3 ? args[3] : nil
func emit(_ text: String) {
    if let f = outFile { try? text.write(toFile: f, atomically: true, encoding: .utf8) } else { print(text) }
}
if !granted {
    if outFile == nil { FileHandle.standardError.write("calendar access denied\n".data(using: .utf8)!) }
    emit("{\"error\":\"denied\"}")
    exit(2)
}
let cal = Calendar.current
let start = cal.startOfDay(for: cal.date(byAdding: .day, value: -back, to: Date())!)
let end = cal.date(byAdding: .day, value: ahead, to: cal.startOfDay(for: Date()))!
let pred = store.predicateForEvents(withStart: start, end: end, calendars: nil)
let iso = ISO8601DateFormatter()
var out: [[String: Any]] = store.events(matching: pred).sorted { $0.startDate < $1.startDate }.map { e in
    ["title": e.title ?? "", "start": iso.string(from: e.startDate), "end": iso.string(from: e.endDate),
     "all_day": e.isAllDay, "calendar": e.calendar.title, "location": e.location ?? "", "kind": "event"]
}
// Incomplete reminders due by the end of the window (overdue ones too): assignments and subscriptions usually live here.
let rsem = DispatchSemaphore(value: 0)
var rgranted = false
if #available(macOS 14.0, *) {
    store.requestFullAccessToReminders { ok, _ in rgranted = ok; rsem.signal() }
} else {
    store.requestAccess(to: .reminder) { ok, _ in rgranted = ok; rsem.signal() }
}
_ = rsem.wait(timeout: .now() + 60)
if rgranted {
    let rp = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: end, calendars: nil)
    let fsem = DispatchSemaphore(value: 0)
    store.fetchReminders(matching: rp) { rs in
        for r in rs ?? [] {
            guard let dc = r.dueDateComponents, let d = cal.date(from: dc) else { continue }
            out.append(["title": r.title ?? "", "start": iso.string(from: d), "end": iso.string(from: d),
                        "all_day": dc.hour == nil, "calendar": r.calendar.title, "location": "", "kind": "reminder"])
        }
        fsem.signal()
    }
    _ = fsem.wait(timeout: .now() + 30)
} else {
    out.append(["title": "", "start": iso.string(from: start), "end": iso.string(from: start), "all_day": true, "calendar": "", "location": "", "kind": "reminders_denied"])
}
let data = try! JSONSerialization.data(withJSONObject: out)
emit(String(data: data, encoding: .utf8)!)


// ---------- apply: writes, driven by a JSON spec ----------
// {"calendars": [{"title", "color"}],
//  "events": [{"calendar", "title", "start": "yyyy-MM-ddTHH:mm", "end", "all_day", "location", "notes",
//              "repeat": "weekly"|"monthly", "until": "yyyy-MM-dd"}],          (skipped when already there)
//  "repeat_until": [{"calendars": [titles], "until": "yyyy-MM-dd" | null}],    (every repeating series in them)
//  "update_calendars": [{"title", "new_title", "color"}],                        (rename / recolor)
//  "delete_events": [{"calendar", "title"}],                                     (every event or series with that title)
//  "delete_calendars": [titles]}                                                 (the calendar and all its events)
func applySpec(_ store: EKEventStore, _ granted: Bool, _ specPath: String, _ outPath: String) -> Never {
    var report: [String: [String]] = ["calendars": [], "created": [], "skipped": [], "updated": [], "errors": []]
    func finish(_ code: Int32) -> Never {
        let data = try! JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted])
        try? data.write(to: URL(fileURLWithPath: outPath))
        exit(code)
    }
    if !granted { report["errors"]!.append("calendar access denied"); finish(2) }
    guard let raw = try? Data(contentsOf: URL(fileURLWithPath: specPath)),
          let spec = try? JSONSerialization.jsonObject(with: raw) as? [String: Any] else { report["errors"]!.append("bad spec"); finish(1) }
    let fmt = DateFormatter(); fmt.locale = Locale(identifier: "en_US_POSIX"); fmt.timeZone = .current
    fmt.dateFormat = "yyyy-MM-dd'T'HH:mm"
    func endOfDay(_ day: String?) -> EKRecurrenceEnd? {   // repeat through the whole of that day
        guard let day = day, let d = fmt.date(from: day + "T23:59") else { return nil }
        return EKRecurrenceEnd(end: d)
    }
    let source = store.sources.first { $0.sourceType == .calDAV && $0.title.lowercased().contains("icloud") }
        ?? store.defaultCalendarForNewEvents?.source ?? store.sources.first { $0.sourceType == .local }
    func calendar(_ title: String) -> EKCalendar? {
        store.calendars(for: .event).first { $0.title == title && $0.allowsContentModifications }
    }
    for c in spec["calendars"] as? [[String: String]] ?? [] {
        let title = c["title"] ?? ""
        if calendar(title) != nil { report["calendars"]!.append("\(title) (existing)"); continue }
        let cal = EKCalendar(for: .event, eventStore: store)
        cal.title = title
        var v: UInt64 = 0; Scanner(string: (c["color"] ?? "#888888").replacingOccurrences(of: "#", with: "")).scanHexInt64(&v)
        cal.cgColor = CGColor(red: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255, blue: CGFloat(v & 0xff) / 255, alpha: 1)
        cal.source = source
        do { try store.saveCalendar(cal, commit: true); report["calendars"]!.append(title) }
        catch { report["errors"]!.append("calendar \(title): \(error.localizedDescription)") }
    }
    for e in spec["events"] as? [[String: Any]] ?? [] {
        let title = e["title"] as? String ?? ""
        guard let cal = calendar(e["calendar"] as? String ?? ""), let start = fmt.date(from: e["start"] as? String ?? ""),
              let end = fmt.date(from: e["end"] as? String ?? "") else { report["errors"]!.append("event \(title): bad calendar/date"); continue }
        let near = store.predicateForEvents(withStart: start.addingTimeInterval(-60), end: start.addingTimeInterval(60), calendars: [cal])
        if store.events(matching: near).contains(where: { $0.title == title && abs($0.startDate.timeIntervalSince(start)) < 1 }) {
            report["skipped"]!.append(title); continue
        }
        let ev = EKEvent(eventStore: store)
        ev.calendar = cal; ev.title = title; ev.startDate = start; ev.endDate = end
        ev.isAllDay = e["all_day"] as? Bool ?? false
        ev.location = e["location"] as? String; ev.notes = e["notes"] as? String
        if let r = e["repeat"] as? String {
            ev.recurrenceRules = [EKRecurrenceRule(recurrenceWith: r == "monthly" ? .monthly : .weekly, interval: 1, end: endOfDay(e["until"] as? String))]
        }
        do { try store.save(ev, span: .futureEvents, commit: false); report["created"]!.append(title) }
        catch { report["errors"]!.append("event \(title): \(error.localizedDescription)") }
    }
    for u in spec["repeat_until"] as? [[String: Any]] ?? [] {
        let cals = (u["calendars"] as? [String] ?? []).compactMap(calendar)
        if cals.isEmpty { report["errors"]!.append("repeat_until: none of those calendars exist"); continue }
        // a series' occurrences share eventIdentifier; change it from its first occurrence so the whole series follows
        let now = Date()
        var first: [String: EKEvent] = [:]
        for ev in store.events(matching: store.predicateForEvents(withStart: now.addingTimeInterval(-86400 * 360), end: now.addingTimeInterval(86400 * 30), calendars: cals))
            where ev.hasRecurrenceRules {
            if let f = first[ev.eventIdentifier], f.startDate <= ev.startDate { continue }
            first[ev.eventIdentifier] = ev
        }
        for ev in first.values {
            guard let old = ev.recurrenceRules?.first else { continue }
            ev.recurrenceRules = [EKRecurrenceRule(recurrenceWith: old.frequency, interval: old.interval, daysOfTheWeek: old.daysOfTheWeek,
                                                   daysOfTheMonth: old.daysOfTheMonth, monthsOfTheYear: old.monthsOfTheYear,
                                                   weeksOfTheYear: old.weeksOfTheYear, daysOfTheYear: old.daysOfTheYear,
                                                   setPositions: old.setPositions, end: endOfDay(u["until"] as? String))]
            do { try store.save(ev, span: .futureEvents, commit: false); report["updated"]!.append("\(ev.title ?? "") (\(ev.calendar.title))") }
            catch { report["errors"]!.append("series \(ev.title ?? ""): \(error.localizedDescription)") }
        }
    }
    func hex(_ h: String) -> CGColor {
        var v: UInt64 = 0; Scanner(string: h.replacingOccurrences(of: "#", with: "")).scanHexInt64(&v)
        return CGColor(red: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255, blue: CGFloat(v & 0xff) / 255, alpha: 1)
    }
    for u in spec["update_calendars"] as? [[String: String]] ?? [] {
        guard let cal = calendar(u["title"] ?? "") else { report["errors"]!.append("update: no calendar \(u["title"] ?? "")"); continue }
        if let t = u["new_title"], !t.isEmpty { cal.title = t }
        if let c = u["color"] { cal.cgColor = hex(c) }
        do { try store.saveCalendar(cal, commit: false); report["updated"]!.append("calendar \(cal.title)") }
        catch { report["errors"]!.append("update \(cal.title): \(error.localizedDescription)") }
    }
    for d in spec["delete_events"] as? [[String: String]] ?? [] {
        guard let cal = calendar(d["calendar"] ?? "") else { report["errors"]!.append("delete: no calendar \(d["calendar"] ?? "")"); continue }
        let now = Date()
        var seen = Set<String>()
        for ev in store.events(matching: store.predicateForEvents(withStart: now.addingTimeInterval(-86400 * 360), end: now.addingTimeInterval(86400 * 1000), calendars: [cal]))
                .sorted(by: { $0.startDate < $1.startDate }) where ev.title == d["title"] && !seen.contains(ev.eventIdentifier) {
            seen.insert(ev.eventIdentifier)   // the first occurrence with .futureEvents takes the whole series
            do { try store.remove(ev, span: .futureEvents, commit: false); report["deleted"] = (report["deleted"] ?? []) + ["\(ev.title ?? "") (\(cal.title))"] }
            catch { report["errors"]!.append("delete \(ev.title ?? ""): \(error.localizedDescription)") }
        }
    }
    do { try store.commit() } catch { report["errors"]!.append("commit: \(error.localizedDescription)") }
    for title in spec["delete_calendars"] as? [String] ?? [] {   // after the commit: removeCalendar commits on its own
        guard let cal = calendar(title) else { report["errors"]!.append("delete: no calendar \(title)"); continue }
        do { try store.removeCalendar(cal, commit: true); report["deleted"] = (report["deleted"] ?? []) + ["calendar \(title)"] }
        catch { report["errors"]!.append("delete calendar \(title): \(error.localizedDescription)") }
    }
    finish(report["errors"]!.isEmpty ? 0 : 3)
}
