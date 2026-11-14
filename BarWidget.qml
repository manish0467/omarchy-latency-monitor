import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Bar button for io.github.manish0467.latency-monitor.
//
// Deliberately thin, like the sibling widgets: it only draws the button and
// forwards the open/close contract to Panel.qml. The Panel owns the data
// (it runs the Python helper), so all real logic lives in helper/ and Panel.
//
// States (from the panel's model):
//   ok           - normal colour, dimmed icon (idleBehaviour "dim")
//   degraded     - amber: a threshold was exceeded, the connection still works
//   down         - red: the public host is unreachable (connection down)
//   unavailable  - dim/info: the helper could not run (no ping, no route data)
//   unknown      - before the first result arrives
//
// Amber colouring: the shell theme exposes no amber token, so the widget
// reads an optional `bar.degraded` shell.toml colour and falls back to a
// plain amber. The urgent (red) colour stays reserved for `down`.
BarWidget {
  id: root
  moduleName: "io.github.manish0467.latency-monitor"

  // ---- State shown on the button (all derived from the panel's model) -----
  readonly property string overall: panelLoader.item ? panelLoader.item.overall : "unknown"
  readonly property string label: panelLoader.item ? panelLoader.item.barLabel : ""
  readonly property string idle: String(setting("idleBehavior", "dim"))   // dim | hide | show

  // Nerd Font glyphs, one per state. Distinct shapes, not only colours, so
  // the states stay recognisable on themes with similar colours.
  readonly property var glyphs: ({
    "ok": "\uf1eb",           // wifi
    "degraded": "\uf071",     // warning triangle
    "down": "\uf057",         // times-circle
    "unavailable": "\uf05a",  // info-circle
    "unknown": "\uf021"       // rotate (before the first result arrives)
  })

  readonly property string glyph: root.glyphs[root.overall] || root.glyphs["unknown"]

  // Amber for the degraded state, theme-adaptable (see header comment).
  readonly property color amber: Commons.Color.pick("bar.degraded", "#d8a657")

  // The label must widen the button: BarIconButton fixes its width to one
  // status slot and draws label + glyph from the slot's centre, so a label
  // wider than the slot bleeds over the neighbouring widgets. Measure the
  // label with a hidden Text using the very font BarIconButton's label uses
  // (it positions with label.implicitWidth too), and grow the slot to fit.
  // (FontMetrics cannot do this: it has no `text` property in Qt 6.)
  Text {
    id: labelMeasure
    visible: false
    textFormat: Text.PlainText
    text: root.label
    font.family: button.fontFamily
    font.pixelSize: Style.bar.iconFont
  }
  readonly property real labelExtra:
    root.label !== "" ? labelMeasure.implicitWidth + Style.space(4) : 0

  // Hidden only when everything is fine and the user asked for that.
  // Degraded, down, unavailable and unknown stay visible.
  visible: !(idle === "hide" && root.overall === "ok")
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ---- Contract with the shell (see develop page: BarWidget requirements) --
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function togglePanel() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }
  function refresh() { if (panelLoader.item) panelLoader.item.refresh(true) }

  // Hand the panel everything it needs from the bar. Re-run whenever one of
  // them changes (the bar and the settings arrive after construction).
  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.glyph
    labelText: root.label
    // Widen the fixed slot by the measured label so label + glyph stay
    // inside the button (see labelExtra above). Vertical bars keep the
    // default slot: their label is hidden anyway.
    slotSize: root.vertical ? Style.bar.statusSlot
                            : Style.bar.statusSlot + root.labelExtra
    // Red (urgent) is reserved for a real outage or an unrunnable helper.
    // Degraded is amber via the foreground override below; everything else
    // keeps the normal bar colour.
    active: root.overall === "down" || root.overall === "unavailable"
    useActiveColor: true
    foreground: root.overall === "degraded" ? root.amber
              : (root.bar ? root.bar.barForeground : Commons.Color.foreground)
    // Fresh and quiet: fade the icon so the bar stays calm (never when
    // degraded, down or worse - those states must stay noticeable).
    opacity: (root.overall === "ok" && root.idle === "dim") ? 0.45 : 1.0
    tooltipText: panelLoader.item ? panelLoader.item.tooltip : "Latency Monitor"

    // Left: panel. Right: refresh now. Middle: nothing.
    onPressed: function(b) {
      if (b === Qt.RightButton) root.refresh()
      else if (b === Qt.LeftButton) root.togglePanel()
    }
  }
}
