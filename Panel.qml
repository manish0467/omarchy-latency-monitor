import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Details panel + data owner for io.github.manish0467.latency-monitor.
//
// Responsibilities (and nothing else):
//   1. Run helper/latency.py and keep its JSON output in `doc`.
//   2. Schedule re-measurements (1-minute poll, manual button).
//   3. Draw the panel: both targets, their history sparklines, thresholds.
// All parsing, classification and history writing live in the helper.
//
// PRIVILEGE NOTE: this plugin runs `ping` and `ip route show default` as the
// plain user, nothing else. It never elevates and never writes outside the
// plugin's state directory.
//
// QML notes for readers coming from C++:
//   * `property x: expr` is a binding; Qt re-evaluates it when inputs change.
//   * `Process`/`Timer` are child objects; their callbacks (`onExited`,
//     `onTriggered`, ...) are signal handlers, like connect().
//   * Every Text here is `Text.PlainText`: host names and helper reasons are
//     untrusted and must never be interpreted as markup.
Panel {
  id: root
  moduleName: "io.github.manish0467.latency-monitor"
  manageIpc: false

  // Injected by BarWidget.injectPanel()
  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  // ------------------------------------------------------------------ model
  readonly property var emptyDoc: ({
    state: "unknown", checked_at: null, busy: false, errors: [],
    gateway: {}, public: {}, history: [], thresholds: {}
  })
  property var doc: emptyDoc
  property bool busy: false
  property bool gotDoc: false
  property string runError: ""        // helper failed to run at all

  readonly property string overall: (runError !== "" && String(doc.state) === "unknown") ? "error" : String(doc.state || "unknown")

  // Amber, theme-adaptable (same token as the bar button).
  readonly property color amber: Commons.Color.pick("bar.degraded", "#d8a657")

  function target(key) { return (doc && doc[key]) ? doc[key] : {} }

  // The number on the bar: the public host's average, rounded (D1: the bar
  // shows the connection gauge; the gateway lives in the panel).
  readonly property string barLabel: {
    var p = root.target("public")
    if (p.avg === null || p.avg === undefined) return ""
    return String(Math.round(p.avg)) + "ms"
  }

  function ms(value) {
    if (value === null || value === undefined) return "?"
    return (Math.round(value * 10) / 10) + " ms"
  }

  function ago(epoch) {
    if (!epoch) return "never"
    var s = Math.max(0, Date.now() / 1000 - epoch)
    if (s < 90) return "just now"
    if (s < 5400) return Math.round(s / 60) + " min ago"
    if (s < 129600) return Math.round(s / 3600) + " h ago"
    return Math.round(s / 86400) + " d ago"
  }

  readonly property string statusLine: {
    if (runError !== "" && String(doc.state) === "unknown") return runError
    if (String(doc.state) === "unknown") return busy ? "Measuring…" : "Not measured yet"
    if (busy) return "Measuring…"
    return "checked " + ago(doc.checked_at)
  }

  readonly property string tooltip: {
    var lines = ["Latency Monitor"]
    if (overall === "unknown") lines.push(busy ? "Measuring…" : "Not measured yet")
    else if (overall === "error") lines.push("Error: " + runError)
    else {
      var p = root.target("public")
      var g = root.target("gateway")
      var pl = p && p.avg !== undefined && p.avg !== null
          ? Math.round(p.avg) + " ms (jitter " + (Math.round(p.mdev * 10) / 10) + ")"
          : (p.state === "down" ? "no reply" : "unavailable")
      var gl = g && g.avg !== undefined && g.avg !== null
          ? Math.round(g.avg) + " ms (jitter " + (Math.round(g.mdev * 10) / 10) + ")"
          : (g.state === "down" ? "no reply" : "unavailable")
      lines.push("public " + ((p && p.host) || "?") + ": " + pl)
      lines.push("gateway " + ((g && g.host) || "?") + ": " + gl)
      if (overall === "degraded") {
        var rs = (p && p.reasons) || []
        var gr = (g && g.reasons) || []
        var all = rs.concat(gr)
        if (all.length > 0) lines.push("degraded: " + all[0])
      }
      if (overall === "down") lines.push("connection down: no reply from " + ((p && p.host) || "?"))
    }
    lines.push("Click: details · Right-click: measure now")
    return lines.join("\n")
  }

  // ------------------------------------------------------------- settings
  // Every option has a default here AND in the helper; see README for the table.
  readonly property string mockName: String(setting("mock", ""))
  readonly property int pollMinutes: Math.max(1, Number(setting("pollMinutes", 1)) || 1)
  readonly property string configJson: JSON.stringify({
    publicHost: String(setting("publicHost", "1.1.1.1")),
    gatewayMs: Number(setting("gatewayMs", 25)),
    gatewayJitterMs: Number(setting("gatewayJitterMs", 10)),
    publicMs: Number(setting("publicMs", 150)),
    publicJitterMs: Number(setting("publicJitterMs", 40))
  })

  // file:///home/me/.config/.../helper/latency.py -> /home/me/.config/...
  readonly property string helperPath: decodeURIComponent(String(Qt.resolvedUrl("helper/latency.py")).replace(/^file:\/\//, ""))

  // `python3 -I` = isolated mode: ignores PYTHON* env vars and the cwd, so
  // nothing but the stdlib and our script can be imported.
  function baseCommand() {
    var cmd = ["python3", "-I", helperPath, "--config", configJson]
    if (mockName !== "") cmd = cmd.concat(["--mock", mockName])
    return cmd
  }

  // ------------------------------------------------------------ open/close
  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function toggle() { root.opened ? close() : open() }
  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function") return bar.switchPanelFrom(barIdentity, direction)
    return false
  }

  // --------------------------------------------------------------- helper
  // Refresh = run `check`. `force` exists for the BarWidget contract; there
  // is no cache in this helper, every check measures. Guard: only one check
  // at a time per widget; the helper additionally takes a file lock so
  // several bars or a shell restart can never overlap either.
  function refresh(force) {
    if (checkProc.running) return
    checkProc.command = baseCommand().concat(["check"])
    gotDoc = false
    busy = true
    killTimer.restart()
    checkProc.running = true
  }

  function accept(text) {
    var parsed
    try { parsed = JSON.parse(text) } catch (e) {
      runError = "The helper returned invalid output"
      return
    }
    if (!parsed || parsed.schema !== 1) {
      runError = "The helper speaks an unsupported schema"
      return
    }
    gotDoc = true
    runError = ""
    doc = parsed
    if (parsed.busy) busyRetry.restart()   // another instance is measuring; re-read soon
  }

  Process {
    id: checkProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.accept(text)
    }
    stderr: StdioCollector { id: checkErr; waitForEnd: true }
    onExited: function(exitCode) {
      root.busy = false
      killTimer.stop()
      if (exitCode !== 0 && !root.gotDoc) {
        var lines = String(checkErr.text).trim().split("\n")
        root.runError = "Helper failed (exit " + exitCode + "): " + lines[lines.length - 1]
      }
    }
  }

  // Safety net: the helper has its own timeouts (~10 s per burst); if it
  // still hangs, kill it so the next refresh can run. Never blocks the shell.
  Timer {
    id: killTimer
    interval: 30000
    onTriggered: {
      checkProc.running = false
      root.busy = false
      root.runError = "The measurement timed out"
    }
  }

  // Slow poll (D4): every minute, floor 1. A check costs ~1 s of pinging.
  Timer {
    interval: root.pollMinutes * 60000
    running: true
    repeat: true
    onTriggered: root.refresh(false)
  }

  // First check. Settings are injected by the bar *after* this object is
  // built, so wait a moment instead of reading them too early.
  Timer {
    id: startupTimer
    interval: 2000
    running: true
    onTriggered: root.refresh(false)
  }

  // Settings edited in shell.json: re-check (helper notices the changed config).
  property bool settingsSeen: false
  onSettingsChanged: {
    if (!settingsSeen) { settingsSeen = true; return }
    settingsDebounce.restart()
  }
  Timer {
    id: settingsDebounce
    interval: 800
    onTriggered: root.refresh(false)
  }

  Timer {
    id: busyRetry
    interval: 15000
    onTriggered: root.refresh(false)
  }

  // ------------------------------------------------------------------- UI
  readonly property color fg: Commons.Color.popups.text
  readonly property color dim: Util.alpha(fg, 0.6)

  function stateColor(state) {
    if (state === "degraded") return root.amber
    if (state === "down" || state === "unavailable") return Commons.Color.urgent
    return root.fg
  }

  function valueLine(t) {
    if (!t || !t.state) return "no data yet"
    if (t.state === "unavailable") return t.reason || "unavailable"
    if (t.state === "down") return "no reply · loss " + Math.round(t.loss || 100) + "%"
    // Loss is called out only when it happens: at 0% it just steals width
    // and the line elides on narrow panels.
    var parts = ["avg " + ms(t.avg), "jitter " + ms(t.mdev),
                 "min " + ms(t.min) + " / max " + ms(t.max)]
    if ((t.loss || 0) > 0) parts.push("loss " + Math.round(t.loss) + "%")
    return parts.join(" · ")
  }

  function hostLine(t) {
    if (!t || !t.host) return ""
    if (t.dev) return t.host + " · " + t.dev
    return t.host
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: false
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)

    content: PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      implicitHeight: column.implicitHeight

      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t, modifiers) {
        if (t === "r") root.refresh(true)
      }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(8)

        // ---- Title row: status + refresh
        Item {
          width: parent.width
          height: Math.max(titleText.implicitHeight, refreshButton.height)

          Column {
            anchors.left: parent.left
            anchors.right: refreshButton.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            Text {
              id: titleText
              text: "Latency Monitor"
              textFormat: Text.PlainText
              color: root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            Text {
              width: parent.width
              text: root.statusLine
              textFormat: Text.PlainText
              elide: Text.ElideRight
              color: root.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
          PanelActionButton {
            id: refreshButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconText: ""
            tooltipText: "Measure now (r)"
            enabled: !root.busy
            opacity: root.busy ? 0.5 : 1.0
            onClicked: root.refresh(true)
          }
        }

        // ---- Errors from the helper (before the cards so they're seen)
        Repeater {
          model: {
            var out = []
            var errs = (root.doc && root.doc.errors) || []
            for (var i = 0; i < errs.length && i < 4; i++) out.push(String(errs[i]))
            return out
          }
          delegate: Text {
            required property string modelData
            width: column.width
            wrapMode: Text.Wrap
            text: modelData
            textFormat: Text.PlainText
            color: root.dim
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
        }

        // ---- The two targets, identical cards
        Repeater {
          model: [{ key: "gateway", title: "Gateway" },
                  { key: "public", title: "Public host" }]

          delegate: Column {
            id: card
            required property var modelData
            readonly property string key: modelData.key
            readonly property var t: root.target(key)
            readonly property var th: {
              var all = (root.doc && root.doc.thresholds) || {}
              return all[key] || {}
            }
            width: column.width
            spacing: Style.space(4)

            // header: name .... host (colour = the target's state)
            Item {
              width: parent.width
              height: Math.max(nameText.implicitHeight, hostText.implicitHeight)

              Text {
                id: nameText
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: card.modelData.title
                textFormat: Text.PlainText
                color: root.stateColor(card.t.state)
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }
              Text {
                id: hostText
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                horizontalAlignment: Text.AlignRight
                elide: Text.ElideMiddle
                text: root.hostLine(card.t)
                textFormat: Text.PlainText
                color: root.dim
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }

            // values line
            Text {
              width: parent.width
              elide: Text.ElideRight
              text: root.valueLine(card.t)
              textFormat: Text.PlainText
              color: root.stateColor(card.t.state)
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }

            // degraded reasons (plain words, under the values)
            Repeater {
              model: {
                var out = []
                var rs = (card.t && card.t.reasons) || []
                for (var i = 0; i < rs.length && i < 3; i++) out.push(String(rs[i]))
                return out
              }
              delegate: Text {
                required property string modelData
                width: column.width
                wrapMode: Text.Wrap
                text: "· " + modelData
                textFormat: Text.PlainText
                color: root.amber
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }

            // history sparkline (D3): one bar per kept sample
            Item {
              id: spark
              width: parent.width
              height: Style.space(26)
              readonly property var samples: {
                var out = []
                var hist = (root.doc && root.doc.history) || []
                for (var i = 0; i < hist.length; i++) {
                  var t = hist[i][card.key]
                  out.push(t && t.avg !== undefined && t.avg !== null ? t.avg : null)
                }
                return out
              }
              readonly property int n: samples.length
              readonly property real maxAvg: {
                var m = 0
                for (var i = 0; i < n; i++)
                  if (samples[i] !== null && samples[i] > m) m = samples[i]
                return m > 0 ? m : 1
              }
              readonly property real slotW: n > 0 ? width / n : 0

              Repeater {
                model: spark.n
                delegate: Rectangle {
                  required property int index
                  x: Math.floor(index * spark.slotW)
                  width: Math.max(1, Math.floor(spark.slotW) - 1)
                  anchors.bottom: parent.bottom
                  height: {
                    var v = spark.samples[index]
                    if (v === null) return 2                    // no reply tick
                    return Math.max(3, Math.round(v / spark.maxAvg * (spark.height - 4)))
                  }
                  color: {
                    var v = spark.samples[index]
                    var th = card.th.ms
                    if (v === null) return Commons.Color.urgent
                    if (th && v > th) return root.amber
                    return root.dim
                  }
                  radius: 1
                }
              }

              Text {
                anchors.centerIn: parent
                visible: spark.n === 0
                text: "no history yet"
                textFormat: Text.PlainText
                color: root.dim
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }
          }
        }

        // ---- Footer: thresholds in plain words so amber is explainable
        Text {
          width: parent.width
          wrapMode: Text.Wrap
          text: {
            var th = (root.doc && root.doc.thresholds) || {}
            var g = th.gateway || {}
            var p = th.public || {}
            return "Amber when gateway avg > " + Math.round(g.ms || 0) + " ms" +
                   " or jitter > " + Math.round(g.jitter || 0) + " ms" +
                   ", public avg > " + Math.round(p.ms || 0) + " ms" +
                   " or jitter > " + Math.round(p.jitter || 0) + " ms" +
                   ", or any packet loss. Red only when the public host is unreachable." +
                   (root.mockName !== "" ? "  ·  mock scenario: " + root.mockName : "")
          }
          textFormat: Text.PlainText
          color: root.dim
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
