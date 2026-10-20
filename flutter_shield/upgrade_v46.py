with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = f.read()

# Widen intraday stop to 1.50% (150 bps) so normal 1% wicks don't stop-hunt, with 2.40% (240 bps) target
core = core.replace(
    "  static const int swingStopBps = 175;\n  static const int swingTargetBps = 350;\n  static const int intradayStopBps = 90;\n  static const int intradayTargetBps = 180;",
    "  static const int swingStopBps = 200;\n  static const int swingTargetBps = 400;\n  static const int intradayStopBps = 150;\n  static const int intradayTargetBps = 240;"
)
with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart (Stop-Hunt Protection & 2.4% / 4.0% Targets)")

with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

# 1. Faster news batching so friend's phone doesn't time out + Max 4 open positions rule
ctrl = ctrl.replace(
    "  static const int scoreBatch = 12;",
    "  static const int scoreBatch = 6;"
).replace(
    "  static const int maxCandidates = 40;",
    "  static const int maxCandidates = 30;\n  static const int maxAutoPositions = 4; // Focus capital on max 4 high-conviction trades"
).replace(
    "  static const String currentVersion = '4.5.0';",
    "  static const String currentVersion = '4.6.0';"
)

# 2. Add proMode state toggle to ShieldController
if "bool proMode = false;" not in ctrl:
    ctrl = ctrl.replace(
        "  bool allowOffHoursStaging = false, tradeSubmitting = false, scanInProgress = false;",
        "  bool allowOffHoursStaging = false, tradeSubmitting = false, scanInProgress = false;\n  bool proMode = false;\n  final Map<String, DateTime> _entryTimes = <String, DateTime>{};\n\n  void toggleProMode() {\n    proMode = !proMode;\n    _prefs.setBool('sh_pro', proMode);\n    notifyListeners();\n  }"
    )
    ctrl = ctrl.replace(
        "    executionMode = p.getString('sh_mode') == 'auto' ? ExecMode.auto : ExecMode.manual;",
        "    executionMode = p.getString('sh_mode') == 'auto' ? ExecMode.auto : ExecMode.manual;\n    proMode = p.getBool('sh_pro') ?? false;"
    )

# 3. Enforce max 4 positions in Autonomous Agent & News Auto Entry
ctrl = ctrl.replace(
    "    if (_agentBusy || executionMode != ExecMode.auto || tradeSubmitting) return;",
    "    if (_agentBusy || executionMode != ExecMode.auto || tradeSubmitting) return;\n    if (positions.length >= AutoRules.maxAutoPositions) return; // Cap concurrent positions to prevent over-diversified bleed"
).replace(
    "    if (executionMode != ExecMode.auto || autoLockReason != null || tradeSubmitting) return;",
    "    if (executionMode != ExecMode.auto || autoLockReason != null || tradeSubmitting) return;\n    if (positions.length >= AutoRules.maxAutoPositions) return;"
)

# 4. Record entry timestamp when a trade executes
ctrl = ctrl.replace(
    "        lastTradeResult = AutoExecuted(id, plan);",
    "        _entryTimes[sym] = DateTime.now().toUtc();\n        lastTradeResult = AutoExecuted(id, plan);"
)

# 5. Fix _atRisk: NEVER panic-sell a winning position or a newly opened trade (<30m old or <0.75% loss)
old_at_risk = """  /// Longs are at risk when sentiment <= 0.22, OR sentiment <= 0.32 while the position is losing money.
  /// Prevents premature panic-selling of profitable positions on minor market noise.
  bool _atRisk(Position p, double sentiment) {
    if (p.isLong) {
      if (sentiment <= 0.22) return true;
      return sentiment <= 0.32 && p.unrealizedPl < 0;
    }
    return sentiment >= 1.0 - NewsRules.bearishSentiment;
  }"""

new_at_risk = """  /// Smart Volatility & Profit Shield:
  /// 1. NEVER panic-sell a profitable position on news (let winners run to TP / trail).
  /// 2. Ignore mild news during the first 30 minutes after entry (prevents 60-second BTCUSD whipsaws).
  /// 3. Require meaningful drawdown (< -0.75%) + very bearish sentiment (<= 0.20) before Auto-Defense cuts early.
  bool _atRisk(Position p, double sentiment) {
    if (!p.isLong) return sentiment >= 1.0 - NewsRules.bearishSentiment;
    if (p.unrealizedPl >= 0) return false; // Never cut a green trade on news noise!
    final sym = normSym(p.symbol);
    final openedAt = _entryTimes[sym];
    if (openedAt != null && DateTime.now().toUtc().difference(openedAt) < const Duration(minutes: 30)) {
      return sentiment <= 0.12; // 30-min grace period after buying
    }
    final lossBps = p.avgEntry > 0 ? ((p.avgEntry - p.currentPrice) * kBpsDenom) ~/ p.avgEntry : 0;
    if (lossBps < 75) return false; // Ignore bid-ask spread & minor <0.75% pullbacks
    return sentiment <= 0.20;
  }"""

if old_at_risk in ctrl:
    ctrl = ctrl.replace(old_at_risk, new_at_risk)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Patched lib/shield_controller.dart (Max 4 Positions, 30m Grace Period, Winner Shield, Pro Mode)")

with open('lib/main.dart', 'r', encoding='utf-8') as f:
    main_code = f.read()

# Update version title in AppBar
main_code = main_code.replace("SHIELD v4.4", "SHIELD v4.6").replace("SHIELD v4.5", "SHIELD v4.6")

# Add SIMPLE / PRO toggle pill in header next to PAPER pill
old_pills = "Pill('📄 PAPER', Pal.amber),"
new_pills = """Pill('📄 PAPER', Pal.amber),
                const SizedBox(width: 6),
                 InkWell(
                  borderRadius: BorderRadius.circular(999),
                  onTap: c.toggleProMode,
                  child: Pill(c.proMode ? '🛠️ PRO' : '✨ SIMPLE', c.proMode ? Pal.sky : Pal.emerald),
                ),"""
if "c.toggleProMode" not in main_code and old_pills in main_code:
    main_code = main_code.replace(old_pills, new_pills, 1)

# Hide Open Orders panel in SIMPLE mode (only show in PRO mode) + add one-tap Flatten All button if needed
old_orders_panel = """        Panel(
          title: '⏳ Open orders (${c.orders.length})',"""
new_orders_panel = """        if (c.proMode)
        Panel(
          title: '⏳ Open orders — Pro View (${c.orders.length})',"""
if "if (c.proMode)" not in main_code and old_orders_panel in main_code:
    main_code = main_code.replace(old_orders_panel, new_orders_panel, 1)

# Simplify _PositionRow when in SIMPLE mode (hide raw SL/limit pip clutter and extra buttons)
old_pos_sub = """            Text('${p.qtyText} sh @ ${usd(p.avgEntry)} -> ${usd(p.currentPrice)}', style: _mono.copyWith(fontSize: 11.5, color: Pal.muted)),
            if (prot != null)
              Padding(
                padding: const EdgeInsets.only(top: 3),
                child: Text(prot, style: _mono.copyWith(fontSize: 11, color: Pal.sky)),
              ),"""

new_pos_sub = """            Text(
              c.proMode
                  ? '${p.qtyText} sh @ ${usd(p.avgEntry)} -> ${usd(p.currentPrice)}'
                  : 'Entry ${usd(p.avgEntry)} · Now ${usd(p.currentPrice)}',
              style: _mono.copyWith(fontSize: 11.5, color: Pal.muted),
            ),
            if (c.proMode && prot != null)
              Padding(
                padding: const EdgeInsets.only(top: 3),
                child: Text(prot, style: _mono.copyWith(fontSize: 11, color: Pal.sky)),
              ),"""

if old_pos_sub in main_code:
    main_code = main_code.replace(old_pos_sub, new_pos_sub, 1)

with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(main_code)
print("[OK] Patched lib/main.dart (SIMPLE / PRO Toggle & Clean Position Cards)")
print("\n🎉 SHIELD v4.6.0 upgrade complete!")
