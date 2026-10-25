// ============================================================================
// PROJECT SHIELD v5.2 — lib/main.dart  (UI shell only)
//
// Imports the pure core and the controller:
//   shield_core.dart        risk math, formatting, models, watch-link types
//   shield_controller.dart  ShieldController (state, polling, flows)
//
// ----------------------------------------------------------------------------
// CONTRACT — everything this file expects from the two imports.
//
// A) Unchanged from v3.0 (same names, same shapes):
//    Pips, RiskRules, Timeframe, ExecMode, KillState, CardStatus, VaultStatus,
//    ConnectionStatus, Account, Position, OrderSummary, FeedEvent, PendingCard,
//    TradeResult (AutoExecuted / Staged / Rejected / Failed), planSummary,
//    usd, priceStr, two, hms, tfLabel, nyTime, mytTime, isNyDst, kFeedMax,
//    WatchLink, PreviewWatchLink, WatchSnapshot, WatchCommand, WatchSendResult,
//    ShieldController(prefs, PreviewWatchLink) with every v3 member
//    (loadLocal, autoConnect, refresh, setMode, triggerTrade, approveCard,
//    rejectCard, dismissCard, runKillSwitch, saveVaultAndConnect, clearVault,
//    toast, onMessage, now, feed, pendingCards, positions, orders, account, ...).
//
// B) NEW in v4.0 (names I assumed — rename here if yours differ):
//    ShieldController
//      bool    updateAvailable;  String? updateVersion;  String? updateNotes;
//      List<ChatMessage> chat;   bool chatBusy;
//      Future<void> sendChat(String text);   void clearChat();
//      List<NewsItem> news;      bool newsLoading;   String? newsError;
//      Future<void> refreshNews();
//      List<DefenseAlert> defenseAlerts;     // held position + bearish news
//      bool isClosing(String symbol);
//      Future<void> closePosition(String symbol);
//    ChatMessage    { bool isUser; String text; DateTime time; List<ChatAction> actions; }
//    ChatAction     { String label; String level; }          // info|ok|warn|error
//    NewsItem       { String id, headline, summary, source; List<String> symbols;
//                     DateTime createdAt; double sentiment; } // -1.0 .. +1.0
//    DefenseAlert   { Position position; NewsItem news; }
//    PendingCard    + ConfluenceScore? confluence
//    ConfluenceScore{ int score; ConfluenceSignal vwap, rsi, volume; }   // 0..100
//    ConfluenceSignal{ String detail; double strength; }                 // 0..1
// ============================================================================

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'shield_chart.dart';
import 'shield_controller.dart';
import 'shield_core.dart';

// ============================================================================
// THEME & HELPERS
// ============================================================================

abstract final class Pal {
  static const bg = Color(0xFF020617); // Dark OLED
  static const surface = Color(0xFF0F172A);
  static const border = Color(0xFF1E293B);
  static const emerald = Color(0xFF10B981);
  static const sky = Color(0xFF0EA5E9);
  static const danger = Color(0xFFDC2626);
  static const indigo = Color(0xFF4F46E5);
  static const amber = Color(0xFFF59E0B);
  static const muted = Color(0xFF94A3B8);
  static const text = Color(0xFFE2E8F0);
  static const dangerSoft = Color(0xFFF87171);
  static const dangerText = Color(0xFFFCA5A5);
}

/// Daily drawdown circuit-breaker threshold shown on the risk gauge (percent).
const double _kHaltPct = 2.5;

Color _a(Color c, double o) => c.withAlpha((o * 255).round());

const TextStyle _mono = TextStyle(fontFamily: 'monospace', fontSize: 12.5, color: Pal.text);

String _short(String id) => id.length > 8 ? id.substring(0, 8) : id;

String _pct(double v, {int dp = 2}) => '${v >= 0 ? '+' : ''}${v.toStringAsFixed(dp)}%';

String _ago(DateTime t) {
  final d = DateTime.now().toUtc().difference(t.toUtc());
  if (d.inSeconds < 60) return 'just now';
  if (d.inMinutes < 60) return '${d.inMinutes}m ago';
  if (d.inHours < 24) return '${d.inHours}h ago';
  return '${d.inDays}d ago';
}

/// "Just now" / "3m ago" / "1h ago" / "2d ago" — measured against a ticking clock.
String _rel(DateTime t, DateTime now) {
  final d = now.toUtc().difference(t.toUtc());
  if (d.inSeconds < 60) return 'Just now';
  if (d.inMinutes < 60) return '${d.inMinutes}m ago';
  if (d.inHours < 24) return '${d.inHours}h ago';
  return '${d.inDays}d ago';
}

// ---- ADAPTERS: the only places that touch model fields v4.3 had to assume. ----
// If your FeedEvent / OrderSummary use different field names, change them here.
DateTime _evTime(FeedEvent e) => e.time;
String _orderSym(OrderSummary o) => o.symbol.toUpperCase();
String _orderId(OrderSummary o) => o.id;
String _legId(dynamic leg) => leg.id as String;

({String label, Color color, IconData icon}) _catStyle(String category) => switch (category) {
      'trade' => (label: 'TRADE', color: Pal.emerald, icon: Icons.swap_vert_rounded),
      'risk' => (label: 'RISK', color: Pal.amber, icon: Icons.speed_rounded),
      'guard' => (label: 'GUARDIAN', color: Pal.dangerSoft, icon: Icons.shield_outlined),
      'ai' => (label: 'AI SCOUT', color: Color(0xFFA78BFA), icon: Icons.auto_awesome),
      _ => (label: 'SYSTEM', color: Pal.muted, icon: Icons.settings_outlined),
    };

/// Confirmation dialog for cancelling. Returns true only on [Confirm Cancel].
Future<bool> _confirmCancelDialog(BuildContext context, {required String symbol, int count = 1}) async {
  final bulk = count > 1;
  final ok = await showDialog<bool>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text(bulk ? 'Cancel all $count open orders for $symbol?' : 'Cancel open order for $symbol?'),
      content: Text(
        bulk
            ? 'Every open order on $symbol (including bracket legs) will be cancelled. Open positions are NOT closed.'
            : 'This cancels the order at Alpaca. Any open position in $symbol is NOT closed.',
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Keep Order')),
        FilledButton(
          style: FilledButton.styleFrom(backgroundColor: Pal.danger, foregroundColor: Colors.white),
          onPressed: () => Navigator.pop(ctx, true),
          child: const Text('Confirm Cancel'),
        ),
      ],
    ),
  );
  return ok == true;
}

Color _levelColor(String level) => switch (level) {
      'ok' => Pal.emerald,
      'warn' => Pal.amber,
      'error' => Pal.dangerSoft,
      _ => Pal.sky,
    };

final GlobalKey<ScaffoldMessengerState> rootMessengerKey = GlobalKey<ScaffoldMessengerState>();

void showToast(String msg, bool error) {
  rootMessengerKey.currentState
    ?..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(
      content: Text(msg),
      backgroundColor: error ? Pal.danger : const Color(0xFF047857),
      behavior: SnackBarBehavior.floating,
      duration: Duration(seconds: error ? 6 : 3),
    ));
}

// ============================================================================
// ENTRY POINT
// ============================================================================

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final prefs = await SharedPreferences.getInstance();
  final link = PreviewWatchLink();
  final c = ShieldController(prefs, link);
  link.phone = c;
  c.onMessage = showToast;
  await c.loadLocal();
  runApp(ShieldApp(c: c));
  unawaited(c.autoConnect());
}

class ShieldApp extends StatelessWidget {
  const ShieldApp({super.key, required this.c});
  final ShieldController c;

  @override
  Widget build(BuildContext context) {
    final base = ThemeData(brightness: Brightness.dark, useMaterial3: true);
    OutlineInputBorder ob(Color color) =>
        OutlineInputBorder(borderRadius: BorderRadius.circular(8), borderSide: BorderSide(color: color));
    return MaterialApp(
      title: 'Shield',
      debugShowCheckedModeBanner: false,
      scaffoldMessengerKey: rootMessengerKey,
      theme: base.copyWith(
        scaffoldBackgroundColor: Pal.bg,
        colorScheme: const ColorScheme.dark(
          primary: Pal.emerald,
          secondary: Pal.sky,
          surface: Pal.surface,
          error: Pal.danger,
          onSurface: Pal.text,
        ),
        appBarTheme: const AppBarTheme(backgroundColor: Pal.surface, elevation: 0, foregroundColor: Pal.text),
        dividerTheme: const DividerThemeData(color: Pal.border),
        bottomNavigationBarTheme: const BottomNavigationBarThemeData(
          backgroundColor: Pal.surface,
          selectedItemColor: Pal.emerald,
          unselectedItemColor: Pal.muted,
          type: BottomNavigationBarType.fixed,
          showUnselectedLabels: true,
          selectedLabelStyle: TextStyle(fontSize: 11),
          unselectedLabelStyle: TextStyle(fontSize: 11),
        ),
        inputDecorationTheme: InputDecorationTheme(
          filled: true,
          fillColor: Pal.bg,
          isDense: true,
          border: ob(Pal.border),
          enabledBorder: ob(Pal.border),
          focusedBorder: ob(Pal.emerald),
          labelStyle: const TextStyle(color: Pal.muted),
          hintStyle: const TextStyle(color: Pal.muted),
        ),
      ),
      home: ListenableBuilder(
        listenable: c,
        builder: (context, _) {
          if (!c.booted || c.autoConnecting) return const _Splash();
          return c.vaultStatus == VaultStatus.valid ? ShieldHome(c: c) : KeyVaultScreen(c: c);
        },
      ),
    );
  }
}

class _Splash extends StatelessWidget {
  const _Splash();
  @override
  Widget build(BuildContext context) => const Scaffold(
        body: Center(
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text('🛡️', style: TextStyle(fontSize: 48)),
            SizedBox(height: 16),
            CircularProgressIndicator(color: Pal.emerald),
            SizedBox(height: 16),
            Text('Connecting to Alpaca…', style: TextStyle(color: Pal.muted)),
          ]),
        ),
      );
}

// ============================================================================
// APP SHELL — AppBar, update banner, 5-tab BottomNavigationBar
// ============================================================================

class ShieldHome extends StatefulWidget {
  const ShieldHome({super.key, required this.c});
  final ShieldController c;
  @override
  State<ShieldHome> createState() => _ShieldHomeState();
}

class _ShieldHomeState extends State<ShieldHome> {
  int _tab = 0;
  bool _bannerDismissed = false;

  ShieldController get c => widget.c;

  void _openVault() => Navigator.of(context)
      .push(MaterialPageRoute<void>(builder: (_) => KeyVaultScreen(c: c, modal: true)));

  void _openWatch() => Navigator.of(context)
      .push(MaterialPageRoute<void>(builder: (_) => WatchPreviewScreen(link: c.watchLink)));

  /// Portfolio "Chart" buttons land here: load the symbol, show the chart in a sheet.
  void _openChart(String symbol) {
    unawaited(c.loadChart(symbol));
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      backgroundColor: Pal.bg,
      constraints: const BoxConstraints(maxWidth: 720),
      builder: (ctx) => FractionallySizedBox(
        heightFactor: .94,
        child: Padding(
          padding: EdgeInsets.only(bottom: MediaQuery.of(ctx).viewInsets.bottom),
          child: Column(children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 6, 0),
              child: Row(children: [
                const Expanded(child: Text('Chart', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 16))),
                IconButton(icon: const Icon(Icons.close, color: Pal.muted), onPressed: () => Navigator.pop(ctx)),
              ]),
            ),
            Expanded(
              child: SingleChildScrollView(
                padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
                child: ShieldChartCard(controller: c),
              ),
            ),
          ]),
        ),
      ),
    );
  }

  void _showUpdate() {
    final v = c.updateVersion;
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Update available'),
        content: Text(
          '${v == null ? 'A newer build of Shield is available.' : 'Shield $v is available.'}\n\n'
          '${c.updateNotes ?? 'Install it from the same channel you got this app from.'}',
        ),
        actions: [TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Close'))],
      ),
    );
  }

  Widget _navIcon(IconData icon, int badge, Color badgeColor) => Badge(
        label: Text('$badge'),
        isLabelVisible: badge > 0,
        backgroundColor: badgeColor,
        textColor: Colors.white,
        child: Icon(icon),
      );

  @override
  Widget build(BuildContext context) {
    final auto = c.executionMode == ExecMode.auto;
    final pending = c.pendingCards.where((x) => x.status == CardStatus.pending).length;
    final defense = c.defenseAlerts.length;

    final Widget keyPill = switch (c.connectionStatus) {
      ConnectionStatus.connected => Pill('Key ..${c.keyTail}', Pal.emerald, onTap: _openVault),
      ConnectionStatus.connecting => Pill('Connecting…', Pal.sky, onTap: _openVault),
      ConnectionStatus.authError => Pill('Key rejected', Pal.danger, onTap: _openVault),
      ConnectionStatus.networkError => Pill('Offline · ..${c.keyTail}', Pal.amber, onTap: _openVault),
      ConnectionStatus.disconnected => Pill('Tap Connect API', Pal.amber, onTap: _openVault),
    };

    return Scaffold(
      appBar: AppBar(
        titleSpacing: 12,
        title: const Text('🛡️ SHIELD v5.2', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17)),
        actions: [
          if (c.updateAvailable)
            IconButton(
              tooltip: 'Update available',
              icon: const Badge(smallSize: 8, backgroundColor: Pal.amber, child: Icon(Icons.system_update_alt, color: Pal.amber)),
              onPressed: _showUpdate,
            ),
          IconButton(tooltip: 'Field Manual', icon: const Icon(Icons.help_outline), onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FieldManualScreen()))),
          IconButton(tooltip: 'Key Vault', icon: const Icon(Icons.vpn_key_outlined), onPressed: _openVault),
          IconButton(tooltip: 'Watch HUD', icon: const Icon(Icons.watch_outlined), onPressed: _openWatch),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(44),
          child: Align(
            alignment: Alignment.centerLeft,
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
              child: Row(children: [
                keyPill,
                const SizedBox(width: 8),
                Pill(auto ? '🤖 AUTO' : '📱 MANUAL', auto ? Pal.emerald : Pal.sky),
                const SizedBox(width: 8),
                const Pill('📄 PAPER', Pal.amber),
                const SizedBox(width: 6),
                 InkWell(
                  borderRadius: BorderRadius.circular(999),
                  onTap: c.toggleProMode,
                  child: Pill(c.proMode ? '🛠️ PRO' : '✨ SIMPLE', c.proMode ? Pal.sky : Pal.emerald),
                ),
              ]),
            ),
          ),
        ),
      ),
      body: Column(children: [
        if (c.updateAvailable && !_bannerDismissed)
          UpdateBanner(c: c, onDetails: _showUpdate, onDismiss: () => setState(() => _bannerDismissed = true)),
        Expanded(
          child: IndexedStack(index: _tab, children: [
            CockpitTab(c: c),
            CopilotTab(c: c),
            NewsTab(c: c),
            TradeTab(c: c),
            PortfolioTab(c: c, onChart: _openChart),
          ]),
        ),
      ]),
      bottomNavigationBar: DecoratedBox(
        decoration: const BoxDecoration(border: Border(top: BorderSide(color: Pal.border))),
        child: BottomNavigationBar(
          currentIndex: _tab,
          onTap: (i) => setState(() => _tab = i),
          items: [
            const BottomNavigationBarItem(icon: Icon(Icons.dashboard_outlined), activeIcon: Icon(Icons.dashboard), label: 'Cockpit'),
            const BottomNavigationBarItem(icon: Icon(Icons.smart_toy_outlined), activeIcon: Icon(Icons.smart_toy), label: 'Copilot'),
            BottomNavigationBarItem(icon: _navIcon(Icons.newspaper, defense, Pal.danger), label: 'News'),
            BottomNavigationBarItem(icon: _navIcon(Icons.show_chart, pending, Pal.sky), label: 'Trade'),
            const BottomNavigationBarItem(icon: Icon(Icons.pie_chart_outline), activeIcon: Icon(Icons.pie_chart), label: 'Portfolio'),
          ],
        ),
      ),
    );
  }
}

class UpdateBanner extends StatelessWidget {
  const UpdateBanner({super.key, required this.c, required this.onDetails, required this.onDismiss});
  final ShieldController c;
  final VoidCallback onDetails, onDismiss;

  @override
  Widget build(BuildContext context) {
    final v = c.updateVersion;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(12, 4, 4, 4),
      decoration: BoxDecoration(color: _a(Pal.sky, .15), border: const Border(bottom: BorderSide(color: Pal.sky))),
      child: Row(children: [
        const Icon(Icons.system_update_alt, size: 18, color: Pal.sky),
        const SizedBox(width: 8),
        Expanded(
          child: Text(v == null ? 'A new Shield update is available' : 'Update available — $v',
              style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
        ),
        TextButton(onPressed: onDetails, child: const Text('Details')),
        IconButton(
          tooltip: 'Dismiss',
          visualDensity: VisualDensity.compact,
          icon: const Icon(Icons.close, size: 18, color: Pal.muted),
          onPressed: onDismiss,
        ),
      ]),
    );
  }
}

// ============================================================================
// SHARED WIDGETS
// ============================================================================

class Panel extends StatelessWidget {
  const Panel({super.key, required this.title, required this.child, this.border = Pal.border, this.trailing});
  final String title;
  final Widget child;
  final Color border;
  final Widget? trailing;
  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(color: Pal.surface, borderRadius: BorderRadius.circular(10), border: Border.all(color: border)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(child: Text(title, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 14))),
            if (trailing != null) trailing!,
          ]),
          const SizedBox(height: 10),
          child,
        ]),
      );
}

class Pill extends StatelessWidget {
  const Pill(this.text, this.color, {super.key, this.onTap});
  final String text;
  final Color color;
  final VoidCallback? onTap;
  @override
  Widget build(BuildContext context) => InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(20),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
          decoration: BoxDecoration(color: _a(color, .15), border: Border.all(color: color), borderRadius: BorderRadius.circular(20)),
          child: Text(text, style: TextStyle(color: color, fontSize: 12, fontWeight: FontWeight.w600)),
        ),
      );
}

class _Tag extends StatelessWidget {
  const _Tag(this.text, this.color);
  final String text;
  final Color color;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
        decoration: BoxDecoration(color: _a(color, .15), borderRadius: BorderRadius.circular(6), border: Border.all(color: _a(color, .7))),
        child: Text(text, style: TextStyle(color: color, fontSize: 11, fontWeight: FontWeight.w700)),
      );
}

class _Header extends StatelessWidget {
  const _Header(this.text, {this.trailing});
  final String text;
  final Widget? trailing;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(2, 4, 2, 8),
        child: Row(children: [
          Expanded(child: Text(text, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 14))),
          if (trailing != null) trailing!,
        ]),
      );
}

class MetricTile extends StatelessWidget {
  const MetricTile(this.label, this.value, {super.key, this.color = Pal.text, this.sub, this.big = false});
  final String label, value;
  final String? sub;
  final Color color;
  final bool big;
  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(color: Pal.surface, borderRadius: BorderRadius.circular(10), border: Border.all(color: Pal.border)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(label, style: const TextStyle(color: Pal.muted, fontSize: 11)),
          const SizedBox(height: 4),
          FittedBox(
            fit: BoxFit.scaleDown,
            alignment: Alignment.centerLeft,
            child: Text(value, style: TextStyle(color: color, fontSize: big ? 30 : 19, fontWeight: FontWeight.w700)),
          ),
          if (sub != null) Text(sub!, style: TextStyle(color: color, fontSize: 12)),
        ]),
      );
}

class GaugeBar extends StatelessWidget {
  const GaugeBar({
    super.key,
    required this.label,
    required this.value,
    required this.footer,
    required this.progress,
    required this.color,
  });
  final String label, value, footer;
  final double progress; // 0..1 (clamped)
  final Color color;
  @override
  Widget build(BuildContext context) => Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Expanded(child: Text(label, style: const TextStyle(color: Pal.muted, fontSize: 11, fontWeight: FontWeight.w600))),
          Text(value, style: TextStyle(color: color, fontSize: 13, fontWeight: FontWeight.w800)),
        ]),
        const SizedBox(height: 6),
        ClipRRect(
          borderRadius: BorderRadius.circular(5),
          child: LinearProgressIndicator(value: progress.clamp(0.0, 1.0).toDouble(), minHeight: 10, backgroundColor: Pal.bg, color: color),
        ),
        const SizedBox(height: 4),
        Text(footer, style: const TextStyle(color: Pal.muted, fontSize: 11)),
      ]);
}

/// Scrollable, width-capped page body used by every non-chat tab.
class _TabScroll extends StatelessWidget {
  const _TabScroll({required this.children, this.onRefresh});
  final List<Widget> children;
  final Future<void> Function()? onRefresh;
  @override
  Widget build(BuildContext context) {
    final list = ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.all(12),
      children: children,
    );
    final body = onRefresh == null
        ? list
        : RefreshIndicator(color: Pal.emerald, backgroundColor: Pal.surface, onRefresh: onRefresh!, child: list);
    return Center(child: ConstrainedBox(constraints: const BoxConstraints(maxWidth: 720), child: body));
  }
}

class SecretField extends StatefulWidget {
  const SecretField({super.key, required this.controller, required this.label, this.hint});
  final TextEditingController controller;
  final String label;
  final String? hint;
  @override
  State<SecretField> createState() => _SecretFieldState();
}

class _SecretFieldState extends State<SecretField> {
  bool _reveal = false;
  @override
  Widget build(BuildContext context) => TextField(
        controller: widget.controller,
        obscureText: !_reveal, // masked by default
        enableSuggestions: false,
        autocorrect: false,
        style: _mono,
        decoration: InputDecoration(
          labelText: widget.label,
          hintText: widget.hint,
          suffixIcon: IconButton(
            icon: Icon(_reveal ? Icons.visibility_off : Icons.visibility, size: 20, color: Pal.muted),
            onPressed: () => setState(() => _reveal = !_reveal),
          ),
        ),
      );
}

// ============================================================================
// TAB 0 — COCKPIT
// ============================================================================

class CockpitTab extends StatelessWidget {
  const CockpitTab({super.key, required this.c});
  final ShieldController c;
  @override
  Widget build(BuildContext context) => _TabScroll(
        onRefresh: c.refresh,
        children: [
          StatusBanner(c: c),
          EquityMetrics(c: c),
          RiskGaugesPanel(c: c),
          ModePanel(c: c),
          KillSwitchPanel(c: c),
        ],
      );
}

class StatusBanner extends StatelessWidget {
  const StatusBanner({super.key, required this.c});
  final ShieldController c;

  @override
  Widget build(BuildContext context) {
    final err = c.connectionStatus == ConnectionStatus.authError || c.connectionStatus == ConnectionStatus.networkError;
    return Panel(
      title: 'Status',
      trailing: Pill(
        c.sessionOpen ? '🟢 US MARKET OPEN' : '⏸️ ${c.sessionMessage.isEmpty ? 'Market status unknown' : c.sessionMessage}',
        c.sessionOpen ? Pal.emerald : Pal.amber,
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if (err)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(bottom: 8),
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(color: _a(Pal.danger, .15), border: Border.all(color: Pal.danger), borderRadius: BorderRadius.circular(8)),
            child: Text(
              '⚠️ ${c.lastError ?? 'Connection problem'}\n'
              'Showing last-known data${c.lastSyncAt == null ? '' : ' from ${hms(mytTime(c.lastSyncAt!))} MYT'}.',
              style: const TextStyle(color: Pal.dangerText, fontSize: 12.5),
            ),
          ),
        if (c.sessionOpen && c.sessionMessage.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Text('Intraday: ${c.sessionMessage}', style: const TextStyle(color: Pal.amber, fontSize: 12)),
          ),
        Text('⚡ ${c.lastEvent}', style: _mono),
        const SizedBox(height: 6),
        ValueListenableBuilder<DateTime>(
          valueListenable: c.now,
          builder: (_, t, __) => Text(
            'MYT: ${hms(mytTime(t))} | ${isNyDst(t) ? 'EDT' : 'EST'}: ${hms(nyTime(t))}',
            style: const TextStyle(color: Pal.muted, fontSize: 12, fontFamily: 'monospace'),
          ),
        ),
      ]),
    );
  }
}

class EquityMetrics extends StatelessWidget {
  const EquityMetrics({super.key, required this.c});
  final ShieldController c;
  @override
  Widget build(BuildContext context) {
    final a = c.account;
    final p = a?.pnl;
    final up = (p?.pnl ?? 0) >= 0;
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(children: [
        MetricTile('MY EQUITY', a == null ? '—' : usd(a.equity), big: true),
        const SizedBox(height: 8),
        Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Expanded(child: MetricTile('BUYING POWER', a == null ? '—' : usd(a.buyingPower), color: Pal.sky)),
          const SizedBox(width: 8),
          Expanded(
            child: MetricTile(
              'TODAY P&L',
              p == null ? '—' : usd(p.pnl, sign: true),
              color: up ? Pal.emerald : Pal.dangerSoft,
              sub: p == null ? null : _pct(p.pct),
            ),
          ),
        ]),
      ]),
    );
  }
}

class RiskGaugesPanel extends StatelessWidget {
  const RiskGaugesPanel({super.key, required this.c});
  final ShieldController c;

  @override
  Widget build(BuildContext context) {
    final a = c.account;
    final eq = a?.equity ?? 0;

    // Gauge 1 — today's P&L against the -2.5% circuit-breaker.
    final pct = a?.pnl.pct ?? 0.0;
    final used = pct < 0 ? (-pct / _kHaltPct) : 0.0; // 0..1+ of the halt limit consumed
    final ddColor = (a?.drawdownHalted ?? false) || used >= 1
        ? Pal.danger
        : used >= .6
            ? Pal.amber
            : Pal.emerald;

    // Gauge 2 — largest single position against the 15% notional cap.
    Pips largest = 0, gross = 0;
    var largestSym = '';
    for (final p in c.positions) {
      final mv = p.marketValue.abs();
      gross += mv;
      if (mv > largest) {
        largest = mv;
        largestSym = p.symbol;
      }
    }
    final cap = RiskRules.notionalCapBps / 100.0; // 15.0
    final largestPct = eq > 0 ? largest / eq * 100.0 : 0.0;
    final grossPct = eq > 0 ? gross / eq * 100.0 : 0.0;
    final capUsed = largestPct / cap;
    final capColor = capUsed >= 1
        ? Pal.danger
        : capUsed >= .8
            ? Pal.amber
            : Pal.emerald;

    return Panel(
      title: '📊 Risk gauges',
      trailing: (a?.drawdownHalted ?? false) ? const Pill('⛔ HALTED', Pal.danger) : null,
      child: Column(children: [
        GaugeBar(
          label: 'TODAY P&L vs −${_kHaltPct.toStringAsFixed(1)}% HALT',
          value: a == null ? '—' : _pct(pct),
          progress: used,
          color: ddColor,
          footer: a == null
              ? 'Waiting for account data'
              : (pct >= 0
                  ? 'Halt at −${_kHaltPct.toStringAsFixed(2)}% · full buffer'
                  : 'Halt at −${_kHaltPct.toStringAsFixed(2)}% · ${(used.clamp(0.0, 1.0) * 100).round()}% of limit used'),
        ),
        const SizedBox(height: 14),
        GaugeBar(
          label: 'LARGEST POSITION vs ${cap.toStringAsFixed(0)}% CAP',
          value: largest == 0 ? 'Flat' : '$largestSym ${largestPct.toStringAsFixed(1)}%',
          progress: capUsed,
          color: capColor,
          footer: 'Cap ${cap.toStringAsFixed(1)}% of equity · Gross exposure ${grossPct.toStringAsFixed(1)}%',
        ),
      ]),
    );
  }
}

class ModePanel extends StatelessWidget {
  const ModePanel({super.key, required this.c});
  final ShieldController c;

  Future<void> _select(BuildContext context, ExecMode m) async {
    if (m == c.executionMode) return;
    if (m == ExecMode.manual) {
      await c.setMode(ExecMode.manual);
      return;
    }
    final lock = c.autoLockReason;
    if (lock != null) {
      c.toast('AUTO is locked: $lock', true);
      return;
    }
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Enable AUTO mode?'),
        content: const Text(
          'In AUTO, a trigger or AI signal submits a (paper) bracket order immediately — no approval card. '
          'Off-hours staging always stays MANUAL. The kill switch returns you to MANUAL.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Pal.emerald, foregroundColor: Colors.black),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Enable AUTO'),
          ),
        ],
      ),
    );
    if (ok == true) await c.setMode(ExecMode.auto);
  }

  @override
  Widget build(BuildContext context) {
    final auto = c.executionMode == ExecMode.auto;
    final lock = c.autoLockReason;
    final tone = auto ? Pal.emerald : Pal.sky;
    return Panel(
      title: '🎛️ Execution mode',
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SizedBox(
          width: double.infinity,
          child: SegmentedButton<ExecMode>(
            showSelectedIcon: false,
            segments: const [
              ButtonSegment<ExecMode>(value: ExecMode.manual, icon: Icon(Icons.touch_app_outlined, size: 18), label: Text('MANUAL')),
              ButtonSegment<ExecMode>(value: ExecMode.auto, icon: Icon(Icons.smart_toy_outlined, size: 18), label: Text('AUTO')),
            ],
            selected: {c.executionMode},
            onSelectionChanged: (s) => _select(context, s.first),
            style: SegmentedButton.styleFrom(
              foregroundColor: Pal.muted,
              backgroundColor: Pal.bg,
              selectedForegroundColor: tone,
              selectedBackgroundColor: _a(tone, .2),
              side: const BorderSide(color: Pal.border),
              padding: const EdgeInsets.symmetric(vertical: 12),
            ),
          ),
        ),
        const SizedBox(height: 8),
        Text(
          auto
              ? 'AUTO: signals submit paper bracket orders immediately.'
              : 'MANUAL: every trade waits for your approval on a staged card.',
          style: const TextStyle(color: Pal.muted, fontSize: 12),
        ),
        if (!auto && lock != null)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text('AUTO locked: $lock', style: const TextStyle(color: Pal.amber, fontSize: 12)),
          ),
      ]),
    );
  }
}

class KillSwitchPanel extends StatelessWidget {
  const KillSwitchPanel({super.key, required this.c});
  final ShieldController c;

  Future<void> _run(BuildContext context) async {
    await c.runKillSwitch();
    if (!context.mounted) return;
    final failed = c.killSwitchState == KillState.failed;
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Pal.surface,
      builder: (_) => Padding(
        padding: const EdgeInsets.all(16),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(
            failed ? '⚠️ Kill switch finished with failures' : '🚨 Kill switch executed',
            style: TextStyle(fontWeight: FontWeight.w700, fontSize: 16, color: failed ? Pal.danger : Pal.text),
          ),
          const SizedBox(height: 10),
          Text(c.killSwitchLastResult ?? '', style: _mono),
          const SizedBox(height: 12),
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final busy = c.killSwitchState == KillState.executing;
    final result = c.killSwitchLastResult;
    final showResult = result != null && (c.killSwitchState == KillState.done || c.killSwitchState == KillState.failed);
    return Panel(
      title: '🚨 Kill switch',
      border: _a(Pal.danger, .6),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        HoldToFlattenButton(busy: busy, onConfirmed: () => _run(context)),
        const SizedBox(height: 8),
        const Text(
          'Press and hold for 2 seconds. Cancels every open order, closes every position, and returns to MANUAL.',
          style: TextStyle(color: Pal.muted, fontSize: 12),
        ),
        if (showResult)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(top: 10),
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(
              color: _a(c.killSwitchState == KillState.failed ? Pal.danger : Pal.amber, .12),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Text('Last run:\n$result', style: _mono.copyWith(fontSize: 11.5)),
          ),
      ]),
    );
  }
}

/// Fills left→right while pressed; fires only after an uninterrupted 2-second hold.
class HoldToFlattenButton extends StatefulWidget {
  const HoldToFlattenButton({super.key, required this.busy, required this.onConfirmed});
  final bool busy;
  final VoidCallback onConfirmed;
  @override
  State<HoldToFlattenButton> createState() => _HoldToFlattenButtonState();
}

class _HoldToFlattenButtonState extends State<HoldToFlattenButton> with SingleTickerProviderStateMixin {
  late final AnimationController _ctl = AnimationController(vsync: this, duration: const Duration(seconds: 2));
  bool _fired = false;

  @override
  void initState() {
    super.initState();
    _ctl.addStatusListener((s) {
      if (s == AnimationStatus.completed && !_fired) {
        _fired = true;
        HapticFeedback.heavyImpact();
        widget.onConfirmed();
        _ctl.reset();
      }
    });
  }

  @override
  void dispose() {
    _ctl.dispose();
    super.dispose();
  }

  void _down() {
    if (widget.busy) return;
    _fired = false;
    HapticFeedback.selectionClick();
    _ctl.forward(from: 0);
  }

  void _release() {
    if (_ctl.status != AnimationStatus.completed) {
      _ctl.animateBack(0, duration: const Duration(milliseconds: 200));
    }
  }

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: 'Hold to flatten all positions and cancel all orders',
      child: GestureDetector(
        onTapDown: (_) => _down(),
        onTapUp: (_) => _release(),
        onTapCancel: _release,
        child: AnimatedBuilder(
          animation: _ctl,
          builder: (_, __) {
            final holding = _ctl.value > 0;
            final label = widget.busy
                ? 'FLATTENING…'
                : holding
                    ? 'KEEP HOLDING…  ${(_ctl.value * 100).round()}%'
                    : '🚨 HOLD TO FLATTEN ALL';
            return Container(
              height: 56,
              width: double.infinity,
              decoration: BoxDecoration(
                color: _a(Pal.danger, .18),
                border: Border.all(color: Pal.danger, width: 1.5),
                borderRadius: BorderRadius.circular(10),
              ),
              clipBehavior: Clip.antiAlias,
              child: Stack(alignment: Alignment.center, children: [
                Align(
                  alignment: Alignment.centerLeft,
                  child: FractionallySizedBox(
                    widthFactor: widget.busy ? 1.0 : _ctl.value,
                    heightFactor: 1.0,
                    child: ColoredBox(color: _a(Pal.danger, widget.busy ? .45 : .85)),
                  ),
                ),
                Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                  if (widget.busy)
                    const Padding(
                      padding: EdgeInsets.only(right: 10),
                      child: SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white)),
                    ),
                  Text(label, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w800, fontSize: 14)),
                ]),
              ]),
            );
          },
        ),
      ),
    );
  }
}

// ============================================================================
// TAB 1 — COPILOT CHAT
// ============================================================================

class CopilotTab extends StatefulWidget {
  const CopilotTab({super.key, required this.c});
  final ShieldController c;
  @override
  State<CopilotTab> createState() => _CopilotTabState();
}

class _CopilotTabState extends State<CopilotTab> {
  final TextEditingController _input = TextEditingController();
  final ScrollController _scroll = ScrollController();
  final FocusNode _focus = FocusNode();
  int _seenCount = 0;
  bool _seenBusy = false;
  bool _seenPending = false;

  /// sendNow = read-only questions fire immediately; order-related prompts only
  /// fill the box so you can review / edit the ticker before sending.
  static const List<({String text, bool sendNow})> _quick = [
    (text: 'How is my risk?', sendNow: true),
    (text: 'What is open right now?', sendNow: true),
    (text: 'Any bad news on my positions?', sendNow: true),
    (text: 'Stage NVDA swing', sendNow: false),
    (text: 'Close ETHUSD', sendNow: false),
  ];

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_scroll.hasClients) return;
      _scroll.animateTo(
        _scroll.position.maxScrollExtent,
        duration: const Duration(milliseconds: 250),
        curve: Curves.easeOut,
      );
    });
  }

  Future<void> _send([String? preset]) async {
    final t = (preset ?? _input.text).trim();
    if (t.isEmpty || widget.c.chatBusy) return;
    if (preset == null) _input.clear();
    await widget.c.sendChat(t);
  }

  void _fill(String t) {
    _input.text = t;
    _input.selection = TextSelection.collapsed(offset: t.length);
    _focus.requestFocus();
  }

  @override
  Widget build(BuildContext context) {
    final c = widget.c;
    final msgs = c.chat;
    final pendingAction = c.pendingCopilotAction;
    final hasPending = pendingAction != null;
    if (msgs.length != _seenCount || c.chatBusy != _seenBusy || hasPending != _seenPending) {
      _seenCount = msgs.length;
      _seenBusy = c.chatBusy;
      _seenPending = hasPending;
      _scrollToEnd();
    }
    return Center(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 720),
        child: Column(children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 8, 6, 0),
            child: Row(children: [
              const Expanded(child: Text('🤖 Shield Copilot', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 15))),
              IconButton(
                tooltip: 'Clear chat',
                icon: const Icon(Icons.delete_sweep_outlined, size: 20, color: Pal.muted),
                onPressed: msgs.isEmpty ? null : c.clearChat,
              ),
            ]),
          ),
          Expanded(
            child: msgs.isEmpty && !c.chatBusy
                ? const _ChatEmpty()
                : ListView.builder(
                    controller: _scroll,
                    padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
                    itemCount: msgs.length + (c.chatBusy ? 1 : 0),
                    itemBuilder: (_, i) => i < msgs.length ? _ChatBubble(m: msgs[i]) : const _TypingBubble(),
                  ),
          ),
          if (pendingAction != null) _CopilotConfirmBanner(c: c, action: pendingAction),
          SizedBox(
            height: 46,
            child: ListView.separated(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
              itemCount: _quick.length,
              separatorBuilder: (_, __) => const SizedBox(width: 8),
              itemBuilder: (_, i) {
                final q = _quick[i];
                return ActionChip(
                  avatar: Icon(q.sendNow ? Icons.bolt : Icons.edit_outlined, size: 14, color: q.sendNow ? Pal.emerald : Pal.sky),
                  label: Text(q.text, style: const TextStyle(fontSize: 12)),
                  backgroundColor: Pal.surface,
                  side: const BorderSide(color: Pal.border),
                  onPressed: c.chatBusy
                      ? null
                      : () {
                          if (q.sendNow) {
                            _send(q.text);
                          } else {
                            _fill(q.text);
                          }
                        },
                );
              },
            ),
          ),
          Container(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 10),
            decoration: const BoxDecoration(color: Pal.surface, border: Border(top: BorderSide(color: Pal.border))),
            child: Row(children: [
              Expanded(
                child: TextField(
                  controller: _input,
                  focusNode: _focus,
                  maxLines: 1,
                  textInputAction: TextInputAction.send,
                  onSubmitted: (_) => _send(),
                  decoration: const InputDecoration(hintText: 'Ask about risk, or "Stage NVDA swing"…'),
                ),
              ),
              const SizedBox(width: 8),
              IconButton.filled(
                tooltip: 'Send',
                style: IconButton.styleFrom(backgroundColor: Pal.emerald, foregroundColor: Colors.black),
                onPressed: c.chatBusy ? null : _send,
                icon: const Icon(Icons.send_rounded, size: 20),
              ),
            ]),
          ),
        ]),
      ),
    );
  }
}

/// High-visibility human-in-the-loop gate: nothing the Copilot proposes runs
/// until the user taps [Approve & Execute].
class _CopilotConfirmBanner extends StatefulWidget {
  const _CopilotConfirmBanner({required this.c, required this.action});
  final ShieldController c;
  final PendingCopilotAction action;
  @override
  State<_CopilotConfirmBanner> createState() => _CopilotConfirmBannerState();
}

class _CopilotConfirmBannerState extends State<_CopilotConfirmBanner> {
  bool _busy = false;

  Future<void> _approve() async {
    if (_busy) return;
    HapticFeedback.mediumImpact();
    setState(() => _busy = true);
    try {
      await widget.c.confirmCopilotAction();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _abort() {
    if (_busy) return;
    widget.c.dismissCopilotAction();
  }

  @override
  Widget build(BuildContext context) {
    final a = widget.action;
    final kind = '${a.kind}'.split('.').last;
    final risky = RegExp(r'close|cancel|flatten|trim|sell|exit', caseSensitive: false).hasMatch(kind);
    final tone = risky ? Pal.danger : Pal.amber;
    final sym = a.symbol;
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.fromLTRB(12, 4, 12, 6),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: _a(tone, .14),
        border: Border.all(color: tone, width: 2),
        borderRadius: BorderRadius.circular(12),
        boxShadow: [BoxShadow(color: _a(tone, .25), blurRadius: 14, spreadRadius: 1)],
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Icon(Icons.gpp_maybe_outlined, color: tone == Pal.danger ? Pal.dangerSoft : tone, size: 20),
          const SizedBox(width: 6),
          Expanded(
            child: Text('CONFIRM COPILOT ACTION',
                style: TextStyle(fontWeight: FontWeight.w800, fontSize: 12, letterSpacing: .6, color: tone == Pal.danger ? Pal.dangerSoft : tone)),
          ),
          _Tag(kind.toUpperCase(), tone == Pal.danger ? Pal.dangerSoft : tone),
          if (sym != null && '$sym'.isNotEmpty) ...[const SizedBox(width: 6), _Tag('$sym', Pal.sky)],
        ]),
        const SizedBox(height: 8),
        Text(a.title, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15.5, color: Colors.white)),
        const SizedBox(height: 4),
        Text(a.summary, style: const TextStyle(fontSize: 13, height: 1.35, color: Pal.text)),
        const SizedBox(height: 4),
        const Text('Nothing is sent to Alpaca until you approve.', style: TextStyle(fontSize: 11, color: Pal.muted)),
        const SizedBox(height: 10),
        Row(children: [
          Expanded(
            child: OutlinedButton(
              style: OutlinedButton.styleFrom(
                foregroundColor: Pal.dangerSoft,
                side: const BorderSide(color: Pal.border),
                padding: const EdgeInsets.symmetric(vertical: 12),
              ),
              onPressed: _busy ? null : _abort,
              child: const Text('Abort', style: TextStyle(fontWeight: FontWeight.w700)),
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            flex: 2,
            child: FilledButton.icon(
              style: FilledButton.styleFrom(
                backgroundColor: Pal.emerald,
                foregroundColor: Colors.black,
                disabledBackgroundColor: _a(Pal.emerald, .5),
                padding: const EdgeInsets.symmetric(vertical: 12),
              ),
              onPressed: _busy ? null : _approve,
              icon: _busy
                  ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.black))
                  : const Icon(Icons.check_circle_outline, size: 18),
              label: Text(_busy ? 'Executing…' : 'Approve & Execute', style: const TextStyle(fontWeight: FontWeight.w800)),
            ),
          ),
        ]),
      ]),
    );
  }
}

class _ChatEmpty extends StatelessWidget {
  const _ChatEmpty();
  @override
  Widget build(BuildContext context) => const Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text('🤖', style: TextStyle(fontSize: 44)),
            SizedBox(height: 10),
            Text('Ask Shield Copilot', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
            SizedBox(height: 6),
            Text(
              'Try "How is my risk?", "Stage NVDA swing" or "Trim TSLA".\nTap a chip below to get started.',
              textAlign: TextAlign.center,
              style: TextStyle(color: Pal.muted, fontSize: 13),
            ),
          ]),
        ),
      );
}

class _ChatBubble extends StatelessWidget {
  const _ChatBubble({required this.m});
  final ChatMessage m;

  @override
  Widget build(BuildContext context) {
    final user = m.isUser;
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Align(
        alignment: user ? Alignment.centerRight : Alignment.centerLeft,
        child: LayoutBuilder(
          builder: (context, cons) => ConstrainedBox(
            constraints: BoxConstraints(maxWidth: cons.maxWidth * .86),
            child: Column(crossAxisAlignment: user ? CrossAxisAlignment.end : CrossAxisAlignment.start, children: [
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
                decoration: BoxDecoration(
                  color: user ? _a(Pal.emerald, .18) : Pal.surface,
                  border: Border.all(color: user ? _a(Pal.emerald, .6) : Pal.border),
                  borderRadius: BorderRadius.only(
                    topLeft: const Radius.circular(14),
                    topRight: const Radius.circular(14),
                    bottomLeft: Radius.circular(user ? 14 : 4),
                    bottomRight: Radius.circular(user ? 4 : 14),
                  ),
                ),
                child: SelectableText(m.text, style: const TextStyle(fontSize: 14, height: 1.35)),
              ),
              if (m.actions.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Wrap(
                    spacing: 6,
                    runSpacing: 6,
                    alignment: user ? WrapAlignment.end : WrapAlignment.start,
                    children: [for (final a in m.actions) _ActionBadge(a: a)],
                  ),
                ),
              Padding(
                padding: const EdgeInsets.only(top: 3),
                child: Text(hms(mytTime(m.time)), style: const TextStyle(color: Pal.muted, fontSize: 10)),
              ),
            ]),
          ),
        ),
      ),
    );
  }
}

class _ActionBadge extends StatelessWidget {
  const _ActionBadge({required this.a});
  final ChatAction a;
  @override
  Widget build(BuildContext context) {
    final color = _levelColor(a.level);
    final icon = switch (a.level) {
      'ok' => Icons.check_circle_outline,
      'warn' => Icons.warning_amber_rounded,
      'error' => Icons.error_outline,
      _ => Icons.bolt,
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(color: _a(color, .14), border: Border.all(color: _a(color, .8)), borderRadius: BorderRadius.circular(8)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(icon, size: 13, color: color),
        const SizedBox(width: 4),
        Flexible(child: Text(a.label, style: TextStyle(color: color, fontSize: 11.5, fontWeight: FontWeight.w700))),
      ]),
    );
  }
}

class _TypingBubble extends StatelessWidget {
  const _TypingBubble();
  @override
  Widget build(BuildContext context) => Align(
        alignment: Alignment.centerLeft,
        child: Container(
          margin: const EdgeInsets.only(bottom: 10),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
          decoration: BoxDecoration(color: Pal.surface, border: Border.all(color: Pal.border), borderRadius: BorderRadius.circular(14)),
          child: const Row(mainAxisSize: MainAxisSize.min, children: [
            SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2, color: Pal.emerald)),
            SizedBox(width: 10),
            Text('Copilot is thinking…', style: TextStyle(color: Pal.muted, fontSize: 13)),
          ]),
        ),
      );
}

// ============================================================================
// TAB 2 — NEWS & DEFENSE
// ============================================================================

class NewsTab extends StatefulWidget {
  const NewsTab({super.key, required this.c});
  final ShieldController c;
  @override
  State<NewsTab> createState() => _NewsTabState();
}

class _NewsTabState extends State<NewsTab> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final c = widget.c;
      if (c.news.isEmpty && !c.newsLoading) unawaited(c.refreshNews());
    });
  }

  @override
  Widget build(BuildContext context) {
    final c = widget.c;
    final alerts = c.defenseAlerts;
    final held = c.positions.map((p) => p.symbol.toUpperCase()).toSet();
    return _TabScroll(
      onRefresh: c.refreshNews,
      children: [
        if (c.newsError != null)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(bottom: 12),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(color: _a(Pal.danger, .15), border: Border.all(color: Pal.danger), borderRadius: BorderRadius.circular(8)),
            child: Text('⚠️ ${c.newsError}', style: const TextStyle(color: Pal.dangerText, fontSize: 12.5)),
          ),
        if (alerts.isNotEmpty) ...[
          _Header('🚨 Defense alerts (${alerts.length})', trailing: const _Tag('HELD + BEARISH', Pal.danger)),
          for (final al in alerts) DefenseCard(c: c, alert: al),
        ],
        _Header(
          '📰 Alpaca breaking news',
          trailing: c.newsLoading
              ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Pal.emerald))
              : Text('${c.news.length} items', style: const TextStyle(color: Pal.muted, fontSize: 12)),
        ),
        if (c.news.isEmpty)
          Panel(
            title: c.newsLoading ? 'Loading news…' : 'No news yet',
            child: const Text('Pull down to refresh. Headlines tagged with a position you hold are highlighted.',
                style: TextStyle(color: Pal.muted, fontSize: 12.5)),
          )
        else
          for (final n in c.news) NewsCard(item: n, held: held),
      ],
    );
  }
}

class DefenseCard extends StatelessWidget {
  const DefenseCard({super.key, required this.c, required this.alert});
  final ShieldController c;
  final DefenseAlert alert;

  @override
  Widget build(BuildContext context) {
    final p = alert.position;
    final n = alert.news;
    final closing = c.isClosing(p.symbol);
    final eq = c.account?.equity ?? 0;
    final expo = eq > 0 ? p.marketValue.abs() / eq * 100.0 : 0.0;
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: _a(Pal.danger, .14),
        border: Border.all(color: Pal.danger, width: 2),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          const Icon(Icons.shield, color: Pal.dangerSoft, size: 20),
          const SizedBox(width: 8),
          Expanded(
            child: Text('DEFENSE ALERT · ${p.symbol}',
                style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 16, color: Colors.white)),
          ),
          const Pill('BEARISH', Pal.danger),
        ]),
        const SizedBox(height: 8),
        Text(n.headline, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 14)),
        if (n.summary.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(n.summary, maxLines: 3, overflow: TextOverflow.ellipsis, style: const TextStyle(color: Pal.dangerText, fontSize: 12.5)),
          ),
        const SizedBox(height: 4),
        Text('${n.source} · ${_ago(n.createdAt)}', style: const TextStyle(color: Pal.muted, fontSize: 11.5)),
        const SizedBox(height: 10),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(8),
          decoration: BoxDecoration(color: Pal.bg, borderRadius: BorderRadius.circular(6), border: Border.all(color: Pal.border)),
          child: Text(
            '${p.isLong ? 'LONG' : 'SHORT'} ${p.qtyText} sh @ ${usd(p.avgEntry)} · P&L ${usd(p.unrealizedPl, sign: true)} · ${expo.toStringAsFixed(1)}% of equity',
            style: _mono.copyWith(fontSize: 12),
          ),
        ),
        const SizedBox(height: 10),
        SizedBox(
          width: double.infinity,
          child: FilledButton.icon(
            style: FilledButton.styleFrom(
              backgroundColor: Pal.danger,
              foregroundColor: Colors.white,
              disabledBackgroundColor: _a(Pal.danger, .5),
              disabledForegroundColor: Colors.white70,
              padding: const EdgeInsets.symmetric(vertical: 14),
            ),
            onPressed: closing ? null : () => c.closePosition(p.symbol),
            icon: closing
                ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                : const Icon(Icons.close, size: 18),
            label: Text(closing ? 'Closing ${p.symbol}…' : 'Close Position ${p.symbol}', style: const TextStyle(fontWeight: FontWeight.w800)),
          ),
        ),
      ]),
    );
  }
}

class NewsCard extends StatelessWidget {
  const NewsCard({super.key, required this.item, required this.held});
  final NewsItem item;
  final Set<String> held;

  @override
  Widget build(BuildContext context) {
    final s = item.sentiment;
    final (label, tone) = s >= .25
        ? ('BULLISH', Pal.emerald)
        : s <= -.25
            ? ('BEARISH', Pal.dangerSoft)
            : ('NEUTRAL', Pal.muted);
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(color: Pal.surface, borderRadius: BorderRadius.circular(10), border: Border.all(color: Pal.border)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Expanded(
            child: Text('${item.source} · ${_ago(item.createdAt)}',
                maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: Pal.muted, fontSize: 11.5)),
          ),
          _Tag(label, tone),
        ]),
        const SizedBox(height: 6),
        Text(item.headline, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 14, height: 1.3)),
        if (item.summary.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(item.summary, maxLines: 3, overflow: TextOverflow.ellipsis, style: const TextStyle(color: Pal.muted, fontSize: 12.5)),
          ),
        if (item.symbols.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Wrap(spacing: 6, runSpacing: 6, children: [
              for (final sym in item.symbols.take(8))
                held.contains(sym.toUpperCase()) ? _Tag('● $sym · held', Pal.amber) : _Tag(sym, Pal.sky),
            ]),
          ),
      ]),
    );
  }
}

// ============================================================================
// TAB 3 — TRADE & STAGED CARDS
// ============================================================================

class TradeTab extends StatelessWidget {
  const TradeTab({super.key, required this.c});
  final ShieldController c;
  @override
  Widget build(BuildContext context) => _TabScroll(
        onRefresh: c.refresh,
        children: [
          ShieldChartCard(controller: c),
          TradePanel(c: c),
          _Header('📱 Staged approval cards (${c.pendingCards.length})'),
          if (c.pendingCards.isEmpty)
            Panel(
              title: 'Nothing staged',
              child: Text(
                c.executionMode == ExecMode.auto
                    ? 'AUTO mode submits orders immediately, so no cards are staged. Switch to MANUAL in the Cockpit to review trades first.'
                    : 'Use Calculate & Route above to stage a trade for approval.',
                style: const TextStyle(color: Pal.muted, fontSize: 12.5),
              ),
            )
          else
            for (final card in c.pendingCards) StagedApprovalCard(c: c, card: card),
        ],
      );
}

class TradePanel extends StatefulWidget {
  const TradePanel({super.key, required this.c});
  final ShieldController c;
  @override
  State<TradePanel> createState() => _TradePanelState();
}

class _TradePanelState extends State<TradePanel> {
  final _ticker = TextEditingController(text: 'NVDA');
  Timeframe _tf = Timeframe.swing;

  @override
  void dispose() {
    _ticker.dispose();
    super.dispose();
  }

  String _resultText(TradeResult r) => switch (r) {
        AutoExecuted(:final orderId, :final plan) => '✅ AUTO order accepted (${_short(orderId)})\n${planSummary(plan)}',
        Staged(:final token, :final plan) => '📱 Staged card $token — approve it below.\n${planSummary(plan)}',
        Rejected(:final reason) => '⛔ $reason',
        Failed(:final error) => '❌ $error',
      };

  Color _resultColor(TradeResult r) => switch (r) {
        AutoExecuted() => Pal.emerald,
        Staged() => Pal.sky,
        Rejected() => Pal.amber,
        Failed() => Pal.danger,
      };

  @override
  Widget build(BuildContext context) {
    final c = widget.c;
    final r = c.lastTradeResult;
    final auto = c.executionMode == ExecMode.auto;
    return Panel(
      title: '🚀 1-Click Precision Trade',
      trailing: Pill(auto ? '🤖 AUTO' : '📱 MANUAL', auto ? Pal.emerald : Pal.sky),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        TextField(
          controller: _ticker,
          style: _mono.copyWith(fontSize: 15),
          textCapitalization: TextCapitalization.characters,
          autocorrect: false,
          inputFormatters: [TextInputFormatter.withFunction((o, n) => n.copyWith(text: n.text.toUpperCase()))],
          decoration: const InputDecoration(labelText: 'Ticker'),
        ),
        const SizedBox(height: 10),
        DropdownButtonFormField<Timeframe>(
          // ignore: deprecated_member_use
          value: _tf,
          dropdownColor: Pal.surface,
          decoration: const InputDecoration(labelText: 'Timeframe'),
          items: const [
            DropdownMenuItem(value: Timeframe.swing, child: Text('SWING (GTC Bracket)')),
            DropdownMenuItem(value: Timeframe.intraday, child: Text('INTRADAY (Day Bracket)')),
          ],
          onChanged: (v) => setState(() => _tf = v ?? _tf),
        ),
        SwitchListTile(
          contentPadding: EdgeInsets.zero,
          activeTrackColor: Pal.amber,
          title: const Text('Allow Off-Hours Staging', style: TextStyle(fontSize: 14)),
          subtitle: const Text('Stages a MANUAL card only (never AUTO). Still needs a live ask.', style: TextStyle(fontSize: 12, color: Pal.muted)),
          value: c.allowOffHoursStaging,
          onChanged: c.setAllowOffHours,
        ),
        const SizedBox(height: 4),
        SizedBox(
          width: double.infinity,
          child: FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Pal.emerald, foregroundColor: Colors.black, padding: const EdgeInsets.symmetric(vertical: 13)),
            onPressed: c.tradeSubmitting ? null : () => c.triggerTrade(ticker: _ticker.text, timeframe: _tf, source: 'Manual Trigger'),
            child: c.tradeSubmitting
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.black))
                : const Text('Calculate & Route', style: TextStyle(fontWeight: FontWeight.w700)),
          ),
        ),
        const SizedBox(height: 6),
        Text(
          auto ? 'AUTO: a passing trade is submitted to Alpaca immediately.' : 'MANUAL: a passing trade becomes an approval card below.',
          style: const TextStyle(color: Pal.muted, fontSize: 11.5),
        ),
        if (r != null)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(top: 10),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(color: _a(_resultColor(r), .12), border: Border.all(color: _resultColor(r)), borderRadius: BorderRadius.circular(8)),
            child: Text(_resultText(r), style: _mono.copyWith(fontSize: 12)),
          ),
      ]),
    );
  }
}

class StagedApprovalCard extends StatelessWidget {
  const StagedApprovalCard({super.key, required this.c, required this.card});
  final ShieldController c;
  final PendingCard card;

  @override
  Widget build(BuildContext context) {
    final live = card.status == CardStatus.pending;
    final busy = card.status == CardStatus.approving;
    final executed = card.status == CardStatus.executed;
    final riskPerShare = card.entry - card.stopLoss;
    final rr = riskPerShare > 0 ? (card.takeProfit - card.entry) / riskPerShare : 0.0;

    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(color: Pal.surface, borderRadius: BorderRadius.circular(12), border: Border.all(color: _a(Pal.sky, .6))),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        // --- header
        Row(children: [
          Text('📈 ${card.ticker}', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 17)),
          const SizedBox(width: 8),
          Pill(tfLabel(card.timeframe), Pal.sky),
          if (card.offHours) ...[const SizedBox(width: 6), const Pill('OFF-HOURS', Pal.amber)],
          const Spacer(),
          Text('${card.qtyText} sh', style: const TextStyle(color: Pal.emerald, fontWeight: FontWeight.w800, fontSize: 15)),
        ]),
        const SizedBox(height: 10),

        // --- visual price ladder
        PriceLadder(target: card.takeProfit, collar: card.limitCollar, entry: card.entry, stop: card.stopLoss),
        const SizedBox(height: 6),
        Wrap(spacing: 14, runSpacing: 4, children: [
          Text('Max risk ${usd(card.maxRisk)}', style: const TextStyle(color: Pal.amber, fontSize: 12, fontWeight: FontWeight.w700)),
          Text('R:R ${rr.toStringAsFixed(2)}', style: const TextStyle(color: Pal.muted, fontSize: 12, fontWeight: FontWeight.w600)),
          Text('Risk/sh ${priceStr(riskPerShare > 0 ? riskPerShare : 0)}', style: const TextStyle(color: Pal.muted, fontSize: 12)),
        ]),
        const SizedBox(height: 10),

        // --- confluence
        ConfluenceBlock(score: card.confluence),
        const SizedBox(height: 8),

        // --- meta
        Text(
          'Micro \$${card.microPrice.toStringAsFixed(2)} · Imb ${card.imbalance >= 0 ? '+' : ''}${card.imbalance.toStringAsFixed(2)} · Spread ${card.spreadBps.toStringAsFixed(1)} bps',
          style: const TextStyle(color: Pal.muted, fontSize: 11.5),
        ),
        const SizedBox(height: 2),
        Text('Source: ${card.source}', style: const TextStyle(color: Pal.muted, fontSize: 11.5)),
        if (card.driftAtApproval != null)
          Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Text('Drift at approval: ${priceStr(card.driftAtApproval!)}', style: const TextStyle(color: Pal.muted, fontSize: 11.5)),
          ),
        const SizedBox(height: 6),
        if (live)
          ValueListenableBuilder<DateTime>(
            valueListenable: c.now,
            builder: (_, t, __) {
              final left = card.expiresAt.difference(t);
              final s = left.inSeconds.clamp(0, 9999).toInt();
              return Text('⏱ ${s ~/ 60}:${two(s % 60)} left',
                  style: TextStyle(color: s < 60 ? Pal.amber : Pal.muted, fontSize: 12, fontWeight: FontWeight.w600));
            },
          ),
        if (busy) const Padding(padding: EdgeInsets.symmetric(vertical: 4), child: LinearProgressIndicator(color: Pal.emerald)),
        if (executed)
          Text('✅ Order accepted${card.orderId == null ? '' : ' (${_short(card.orderId!)})'}', style: const TextStyle(color: Pal.emerald, fontSize: 12.5)),
        if (card.errorMessage != null && !live && !busy)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(top: 4),
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(
              color: _a(card.status == CardStatus.failed ? Pal.danger : Pal.amber, .15),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Text(
              '${card.status == CardStatus.stale ? '🕒 Price moved too far — re-stage. ' : ''}${card.errorMessage}',
              style: const TextStyle(fontSize: 12.5),
            ),
          ),
        const SizedBox(height: 10),

        // --- actions
        if (live || busy)
          Row(children: [
            Expanded(
              child: FilledButton(
                style: FilledButton.styleFrom(backgroundColor: Pal.emerald, foregroundColor: Colors.black, padding: const EdgeInsets.symmetric(vertical: 12)),
                onPressed: busy ? null : () => c.approveCard(card.token),
                child: const Text('✅ Approve Fill', style: TextStyle(fontWeight: FontWeight.w700)),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: OutlinedButton(
                style: OutlinedButton.styleFrom(
                  foregroundColor: Pal.dangerSoft,
                  side: const BorderSide(color: Pal.border),
                  padding: const EdgeInsets.symmetric(vertical: 12),
                ),
                onPressed: busy ? null : () => c.rejectCard(card.token),
                child: const Text('❌ Reject'),
              ),
            ),
          ])
        else
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(onPressed: () => c.dismissCard(card.token), child: const Text('Dismiss')),
          ),
      ]),
    );
  }
}

/// Vertical ladder, highest price on top: Target → Collar → Entry → Stop.
class PriceLadder extends StatelessWidget {
  const PriceLadder({super.key, required this.target, required this.collar, required this.entry, required this.stop});
  final Pips target, collar, entry, stop;

  String _delta(Pips p) => (p == entry || entry <= 0) ? '—' : _pct((p - entry) * 100.0 / entry);

  @override
  Widget build(BuildContext context) {
    final rows = <({String label, Pips price, Color color, String note})>[
      (label: 'TARGET', price: target, color: Pal.sky, note: 'Take-profit'),
      (label: 'COLLAR', price: collar, color: Pal.emerald, note: 'Max buy'),
      (label: 'ENTRY', price: entry, color: Pal.text, note: 'Ask when staged'),
      (label: 'STOP', price: stop, color: Pal.dangerSoft, note: 'Stop-loss'),
    ];
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(color: Pal.bg, borderRadius: BorderRadius.circular(8), border: Border.all(color: Pal.border)),
      child: Column(children: [
        for (var i = 0; i < rows.length; i++)
          SizedBox(
            height: 36,
            child: Row(children: [
              SizedBox(
                width: 24,
                child: CustomPaint(painter: _RailPainter(color: rows[i].color, first: i == 0, last: i == rows.length - 1)),
              ),
              const SizedBox(width: 6),
              SizedBox(
                width: 58,
                child: Text(rows[i].label, style: TextStyle(color: rows[i].color, fontWeight: FontWeight.w800, fontSize: 11.5)),
              ),
              Expanded(
                child: Text(rows[i].note, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: Pal.muted, fontSize: 11)),
              ),
              Text(priceStr(rows[i].price), style: _mono.copyWith(fontWeight: FontWeight.w800, color: rows[i].color)),
              SizedBox(
                width: 60,
                child: Text(_delta(rows[i].price), textAlign: TextAlign.right, style: TextStyle(color: rows[i].color, fontSize: 11.5)),
              ),
            ]),
          ),
      ]),
    );
  }
}

class _RailPainter extends CustomPainter {
  _RailPainter({required this.color, required this.first, required this.last});
  final Color color;
  final bool first, last;

  @override
  void paint(Canvas canvas, Size size) {
    final cx = size.width / 2;
    final cy = size.height / 2;
    final line = Paint()
      ..color = _a(Pal.muted, .4)
      ..strokeWidth = 2;
    canvas.drawLine(Offset(cx, first ? cy : 0), Offset(cx, last ? cy : size.height), line);
    canvas.drawCircle(Offset(cx, cy), 6, Paint()..color = color);
    canvas.drawCircle(
      Offset(cx, cy),
      6,
      Paint()
        ..color = Pal.bg
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2,
    );
  }

  @override
  bool shouldRepaint(_RailPainter old) => old.color != color || old.first != first || old.last != last;
}

class ConfluenceBlock extends StatelessWidget {
  const ConfluenceBlock({super.key, required this.score});
  final ConfluenceScore? score;

  Color _tone(double v) => v >= .66
      ? Pal.emerald
      : v >= .33
          ? Pal.amber
          : Pal.dangerSoft;

  Widget _row(String label, ConfluenceSignal s) => Padding(
        padding: const EdgeInsets.only(top: 6),
        child: Row(children: [
          SizedBox(width: 36, child: Text(label, style: const TextStyle(color: Pal.muted, fontSize: 11, fontWeight: FontWeight.w700))),
          Expanded(
            flex: 3,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(3),
              child: LinearProgressIndicator(value: s.strength.clamp(0.0, 1.0).toDouble(), minHeight: 6, backgroundColor: Pal.surface, color: _tone(s.strength)),
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            flex: 4,
            child: Text(s.detail, textAlign: TextAlign.right, maxLines: 1, overflow: TextOverflow.ellipsis, style: _mono.copyWith(fontSize: 11)),
          ),
        ]),
      );

  @override
  Widget build(BuildContext context) {
    final s = score;
    if (s == null) {
      return Container(
        width: double.infinity,
        padding: const EdgeInsets.all(10),
        decoration: BoxDecoration(color: Pal.bg, borderRadius: BorderRadius.circular(8), border: Border.all(color: Pal.border)),
        child: const Text('CONFLUENCE unavailable — no intraday bars for this ticker.', style: TextStyle(color: Pal.muted, fontSize: 11.5)),
      );
    }
    final tone = _tone(s.score / 100.0);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(color: Pal.bg, borderRadius: BorderRadius.circular(8), border: Border.all(color: Pal.border)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          const Expanded(child: Text('CONFLUENCE SCORE', style: TextStyle(color: Pal.muted, fontSize: 10.5, fontWeight: FontWeight.w700))),
          Text('${s.score}/100', style: TextStyle(color: tone, fontSize: 16, fontWeight: FontWeight.w800)),
        ]),
        const SizedBox(height: 6),
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(value: (s.score / 100.0).clamp(0.0, 1.0).toDouble(), minHeight: 8, backgroundColor: Pal.surface, color: tone),
        ),
        _row('VWAP', s.vwap),
        _row('RSI', s.rsi),
        _row('VOL', s.volume),
      ]),
    );
  }
}

// ============================================================================
// TAB 4 — PORTFOLIO & FEED
// ============================================================================

class PortfolioTab extends StatelessWidget {
  const PortfolioTab({super.key, required this.c, required this.onChart});
  final ShieldController c;
  final void Function(String symbol) onChart;

  @override
  Widget build(BuildContext context) {
    final eq = c.account?.equity ?? 0;
    final cap = RiskRules.notionalCapBps / 100.0;

    // Symbols with 2+ open orders get a one-tap "cancel all" shortcut.
    final perSymbol = <String, int>{};
    for (final o in c.orders) {
      final s = _orderSym(o);
      perSymbol[s] = (perSymbol[s] ?? 0) + 1;
    }
    final multi = perSymbol.entries.where((e) => e.value > 1).toList();

    return _TabScroll(
      onRefresh: c.refresh,
      children: [
        Panel(
          title: '📂 Open positions (${c.positions.length})',
          child: c.positions.isEmpty
              ? const Text('Account is flat.', style: TextStyle(color: Pal.muted))
              : Column(children: [for (final p in c.positions) _PositionRow(c: c, p: p, equity: eq, cap: cap, onChart: onChart)]),
        ),
        if (c.proMode)
        Panel(
          title: '⏳ Open orders — Pro View (${c.orders.length})',
          child: c.orders.isEmpty
              ? const Text('No open orders.', style: TextStyle(color: Pal.muted))
              : Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  if (multi.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: Wrap(spacing: 8, runSpacing: 6, children: [
                        for (final m in multi)
                          TextButton.icon(
                            style: TextButton.styleFrom(
                              foregroundColor: Pal.dangerSoft,
                              visualDensity: VisualDensity.compact,
                              side: BorderSide(color: _a(Pal.danger, .6)),
                            ),
                            icon: const Icon(Icons.layers_clear_outlined, size: 16),
                            label: Text('Cancel all ${m.value} ${m.key} orders', style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700)),
                            onPressed: () async {
                              final ok = await _confirmCancelDialog(context, symbol: m.key, count: m.value);
                              if (!ok) return;
                              final r = await c.cancelOrdersForSymbol(m.key);
                              showToast(r.message, !r.ok);
                            },
                          ),
                      ]),
                    ),
                  for (final o in c.orders) _OrderRow(key: ValueKey(_orderId(o)), c: c, o: o, onChart: onChart),
                ]),
        ),
        Panel(
          title: '📜 Trade history (${c.closedTrades.length})',
          trailing: _TradeScorecard(trades: c.closedTrades),
          child: c.closedTrades.isEmpty
              ? const Text('No completed round-trip trades in recent history.', style: TextStyle(color: Pal.muted))
              : Column(children: [
                  for (final t in c.closedTrades.take(25)) _ClosedTradeRow(t: t, onChart: onChart),
                ]),
        ),
        Panel(
          title: '🕘 Activity timeline',
          trailing: Text('last $kFeedMax', style: const TextStyle(color: Pal.muted, fontSize: 11.5)),
          child: ValueListenableBuilder<DateTime>(
            valueListenable: c.now,
            builder: (_, now, __) => _ActivityTimeline(events: c.feed, now: now),
          ),
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Activity timeline (replaces the raw "[HH:MM:SS] ..." log)
// ---------------------------------------------------------------------------

class _ActivityTimeline extends StatefulWidget {
  const _ActivityTimeline({required this.events, required this.now});
  final List<FeedEvent> events;
  final DateTime now;
  @override
  State<_ActivityTimeline> createState() => _ActivityTimelineState();
}

class _ActivityTimelineState extends State<_ActivityTimeline> {
  static const int _collapsed = 20;
  bool _all = false;

  @override
  Widget build(BuildContext context) {
    final ev = widget.events;
    if (ev.isEmpty) return const Text('No activity yet.', style: TextStyle(color: Pal.muted));
    final shown = _all ? ev : ev.take(_collapsed).toList();
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      for (var i = 0; i < shown.length; i++)
        _TimelineItem(e: shown[i], now: widget.now, last: i == shown.length - 1),
      if (ev.length > _collapsed)
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton(
            onPressed: () => setState(() => _all = !_all),
            child: Text(_all ? 'Show less' : 'Show all ${ev.length} events'),
          ),
        ),
    ]);
  }
}

class _TimelineItem extends StatelessWidget {
  const _TimelineItem({required this.e, required this.now, required this.last});
  final FeedEvent e;
  final DateTime now;
  final bool last;

  @override
  Widget build(BuildContext context) {
    final st = _catStyle(e.category);
    final titleColor = switch (e.level) {
      'error' => Pal.dangerSoft,
      'warn' => Pal.amber,
      _ => Pal.text,
    };
    final sub = e.cleanSubtitle;
    return IntrinsicHeight(
      child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SizedBox(
          width: 28,
          child: Column(children: [
            Container(
              width: 22,
              height: 22,
              decoration: BoxDecoration(color: _a(st.color, .18), shape: BoxShape.circle, border: Border.all(color: st.color)),
              child: Icon(st.icon, size: 12, color: st.color),
            ),
            if (!last) Expanded(child: Container(width: 2, margin: const EdgeInsets.symmetric(vertical: 2), color: Pal.border)),
          ]),
        ),
        const SizedBox(width: 6),
        Expanded(
          child: Container(
            margin: const EdgeInsets.only(bottom: 8),
            padding: const EdgeInsets.fromLTRB(10, 8, 10, 8),
            decoration: BoxDecoration(
              color: Pal.bg,
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Pal.border),
            ),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                _Tag(st.label, st.color),
                const Spacer(),
                Text(_rel(_evTime(e), now), style: const TextStyle(color: Pal.muted, fontSize: 11)),
              ]),
              const SizedBox(height: 6),
              Text(e.cleanTitle, style: TextStyle(color: titleColor, fontWeight: FontWeight.w700, fontSize: 13, height: 1.3)),
              if (sub.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text(sub, style: const TextStyle(color: Pal.muted, fontSize: 12, height: 1.3)),
                ),
            ]),
          ),
        ),
      ]),
    );
  }
}

// ---------------------------------------------------------------------------
// Position + Order rows
// ---------------------------------------------------------------------------

class _TradeScorecard extends StatelessWidget {
  const _TradeScorecard({required this.trades});
  final List<ClosedTrade> trades;

  @override
  Widget build(BuildContext context) {
    if (trades.isEmpty) return const SizedBox.shrink();
    final wins = trades.where((t) => t.isWin).length;
    final losses = trades.length - wins;
    final net = trades.fold<int>(0, (a, b) => a + b.pnlPips);
    final up = net >= 0;
    return Row(mainAxisSize: MainAxisSize.min, children: [
      Text('${wins}W / ${losses}L · ', style: const TextStyle(color: Pal.muted, fontSize: 11.5, fontWeight: FontWeight.w700)),
      Text(usd(net, sign: true), style: TextStyle(color: up ? Pal.emerald : Pal.dangerSoft, fontSize: 12, fontWeight: FontWeight.w800)),
    ]);
  }
}

class _ClosedTradeRow extends StatelessWidget {
  const _ClosedTradeRow({required this.t, required this.onChart});
  final ClosedTrade t;
  final void Function(String symbol) onChart;

  @override
  Widget build(BuildContext context) {
    final up = t.isWin;
    final tone = up ? Pal.emerald : Pal.dangerSoft;
    final local = t.closedAt.toLocal();
    final ts = '${two(local.month)}/${two(local.day)} ${two(local.hour)}:${two(local.minute)}';
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(10, 8, 8, 8),
      decoration: BoxDecoration(color: Pal.bg, borderRadius: BorderRadius.circular(8), border: Border.all(color: Pal.border)),
      child: Row(children: [
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Text(t.symbol, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 14)),
              const SizedBox(width: 6),
              _Tag(up ? 'WIN' : 'LOSS', tone),
              const SizedBox(width: 6),
              Text(ts, style: const TextStyle(color: Pal.muted, fontSize: 11)),
            ]),
            const SizedBox(height: 3),
            Text('Bought ${t.qtyText} @ ${usd(t.buyPrice)}  →  Sold @ ${usd(t.sellPrice)}',
                style: _mono.copyWith(fontSize: 11.5, color: Pal.muted)),
          ]),
        ),
        Column(crossAxisAlignment: CrossAxisAlignment.end, children: [
          Text(usd(t.pnlPips, sign: true), style: TextStyle(color: tone, fontWeight: FontWeight.w800, fontSize: 14)),
          const SizedBox(height: 2),
          Text('${t.pnlPct >= 0 ? '+' : ''}${t.pnlPct.toStringAsFixed(2)}%',
              style: _mono.copyWith(color: tone, fontSize: 11, fontWeight: FontWeight.w700)),
        ]),
        IconButton(
          tooltip: 'Chart ${t.symbol}',
          visualDensity: VisualDensity.compact,
          icon: const Icon(Icons.candlestick_chart_outlined, color: Pal.sky, size: 18),
          onPressed: () => onChart(t.symbol),
        ),
      ]),
    );
  }
}

class _PositionRow extends StatelessWidget {
  const _PositionRow({required this.c, required this.p, required this.equity, required this.cap, required this.onChart});
  final ShieldController c;
  final Position p;
  final Pips equity;
  final double cap;
  final void Function(String symbol) onChart;

  Future<void> _confirmClose(BuildContext context) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Sell / Close ${p.symbol}?'),
        content: Text(
          'This cancels any open protective orders for ${p.symbol} and market-closes '
          '${p.qtyText} sh (Current P&L: ${usd(p.unrealizedPl, sign: true)}).',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Keep')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Pal.danger, foregroundColor: Colors.white),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Confirm Sell'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    final res = await c.closePosition(p.symbol);
    showToast(res.message, !res.ok);
  }

  @override
  Widget build(BuildContext context) {
    final pct = equity > 0 ? p.marketValue.abs() / equity * 100.0 : 0.0;
    final Color tone = pct > cap ? Pal.danger : (pct >= cap * .8 ? Pal.amber : Pal.emerald);
    final up = p.unrealizedPl >= 0;
    final closing = c.isClosing(p.symbol);
    final prot = c.protectionSummary(p.symbol);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(10, 10, 6, 10),
      decoration: BoxDecoration(color: Pal.bg, borderRadius: BorderRadius.circular(8), border: Border.all(color: Pal.border)),
      child: Row(children: [
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Text(p.symbol, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15)),
              const SizedBox(width: 6),
              Text(p.isLong ? 'LONG' : 'SHORT', style: const TextStyle(color: Pal.muted, fontSize: 10.5, fontWeight: FontWeight.w700)),
            ]),
            const SizedBox(height: 2),
            Text(
              c.proMode
                  ? '${p.qtyText} sh @ ${usd(p.avgEntry)} -> ${usd(p.currentPrice)}'
                  : 'Entry ${usd(p.avgEntry)} · Now ${usd(p.currentPrice)}',
              style: _mono.copyWith(fontSize: 11.5, color: Pal.muted),
            ),
            if (prot != null)
              Padding(
                padding: const EdgeInsets.only(top: 3),
                child: Text(prot, style: _mono.copyWith(fontSize: 11, color: Pal.sky)),
              ),
          ]),
        ),
        Column(crossAxisAlignment: CrossAxisAlignment.end, children: [
          Text(usd(p.unrealizedPl, sign: true), style: TextStyle(color: up ? Pal.emerald : Pal.dangerSoft, fontWeight: FontWeight.w800, fontSize: 14)),
          const SizedBox(height: 4),
          Pill(pct > cap ? '! ${pct.toStringAsFixed(1)}% eq' : '${pct.toStringAsFixed(1)}% eq', tone),
        ]),
        IconButton(
          tooltip: 'Chart ${p.symbol}',
          visualDensity: VisualDensity.compact,
          icon: const Icon(Icons.candlestick_chart_outlined, color: Pal.sky),
          onPressed: () => onChart(p.symbol),
        ),
        IconButton(
          tooltip: 'Sell / Close ${p.symbol}',
          visualDensity: VisualDensity.compact,
          icon: closing
              ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Pal.dangerSoft))
              : const Icon(Icons.sell_outlined, color: Pal.dangerSoft),
          onPressed: closing ? null : () => _confirmClose(context),
        ),
      ]),
    );
  }
}

class _OrderRow extends StatefulWidget {
  const _OrderRow({super.key, required this.c, required this.o, required this.onChart});
  final ShieldController c;
  final OrderSummary o;
  final void Function(String symbol) onChart;
  @override
  State<_OrderRow> createState() => _OrderRowState();
}

class _OrderRowState extends State<_OrderRow> {
  String? _busyId; // id currently being cancelled

  Future<void> _cancel(String id) async {
    if (_busyId != null) return;
    final sym = _orderSym(widget.o);
    final ok = await _confirmCancelDialog(context, symbol: sym);
    if (!ok || !mounted) return;
    setState(() => _busyId = id);
    try {
      final r = await widget.c.cancelOrderById(id, symbol: sym);
      showToast(r.message, !r.ok);
    } finally {
      if (mounted) setState(() => _busyId = null);
    }
  }

  Widget _cancelButton({required String id, bool compact = false}) {
    final busy = _busyId == id;
    return FilledButton.icon(
      style: FilledButton.styleFrom(
        backgroundColor: Pal.danger,
        foregroundColor: Colors.white,
        disabledBackgroundColor: _a(Pal.danger, .45),
        disabledForegroundColor: Colors.white70,
        padding: EdgeInsets.symmetric(horizontal: compact ? 10 : 14, vertical: compact ? 6 : 10),
        minimumSize: Size(0, compact ? 32 : 40),
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
      ),
      onPressed: _busyId != null ? null : () => _cancel(id),
      icon: busy
          ? const SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
          : const Icon(Icons.cancel_outlined, size: 16),
      label: Text(busy ? 'Cancelling…' : 'Cancel Order', style: TextStyle(fontWeight: FontWeight.w800, fontSize: compact ? 12 : 13)),
    );
  }

  @override
  Widget build(BuildContext context) {
    final o = widget.o;
    final sym = _orderSym(o);
    final hasLegs = o.legs.isNotEmpty;

    final actions = Row(children: [
      OutlinedButton.icon(
        style: OutlinedButton.styleFrom(
          foregroundColor: Pal.sky,
          side: BorderSide(color: _a(Pal.sky, .7)),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          minimumSize: const Size(0, 40),
        ),
        onPressed: () => widget.onChart(sym),
        icon: const Icon(Icons.candlestick_chart_outlined, size: 16),
        label: const Text('Chart', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 13)),
      ),
      const SizedBox(width: 8),
      Expanded(child: _cancelButton(id: _orderId(o))),
    ]);

    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(10, 8, 10, 10),
      decoration: BoxDecoration(color: Pal.bg, borderRadius: BorderRadius.circular(8), border: Border.all(color: Pal.border)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if (!hasLegs)
          Padding(padding: const EdgeInsets.symmetric(vertical: 4), child: Text('⏳ ${o.line}', style: _mono))
        else
          Theme(
            data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
            child: ExpansionTile(
              tilePadding: EdgeInsets.zero,
              childrenPadding: EdgeInsets.zero,
              iconColor: Pal.muted,
              collapsedIconColor: Pal.muted,
              title: Text('⏳ ${o.line}', style: _mono),
              subtitle: Text('${o.legs.length} bracket legs — tap to expand', style: const TextStyle(color: Pal.muted, fontSize: 11)),
              children: [
                for (final l in o.legs)
                  Padding(
                    padding: const EdgeInsets.only(left: 12, bottom: 6),
                    child: Row(children: [
                      Expanded(child: Text('↳ ${l.line}', style: _mono.copyWith(color: Pal.muted))),
                      const SizedBox(width: 8),
                      _cancelButton(id: _legId(l), compact: true),
                    ]),
                  ),
              ],
            ),
          ),
        const SizedBox(height: 6),
        actions,
      ]),
    );
  }
}

// ============================================================================
// KEY VAULT
// ============================================================================

class KeyVaultScreen extends StatefulWidget {
  const KeyVaultScreen({super.key, required this.c, this.modal = false});
  final ShieldController c;
  final bool modal;
  @override
  State<KeyVaultScreen> createState() => _KeyVaultScreenState();
}

class _KeyVaultScreenState extends State<KeyVaultScreen> {
  late final TextEditingController _ak, _as, _gk, _tt, _tc;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    final v = widget.c.vault;
    _ak = TextEditingController(text: v.alpacaKey);
    _as = TextEditingController(text: v.alpacaSecret);
    _gk = TextEditingController(text: v.geminiKey);
    _tt = TextEditingController(text: v.tgToken);
    _tc = TextEditingController(text: v.tgChat);
  }

  @override
  void dispose() {
    for (final t in [_ak, _as, _gk, _tt, _tc]) {
      t.dispose();
    }
    super.dispose();
  }

  Future<void> _save() async {
    if (_ak.text.trim().isEmpty || _as.text.trim().isEmpty) {
      setState(() => _error = 'Alpaca Key ID and Secret Key are both required.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final err = await widget.c.saveVaultAndConnect(
      alpacaKey: _ak.text,
      alpacaSecret: _as.text,
      geminiKey: _gk.text,
      tgToken: _tt.text,
      tgChat: _tc.text,
    );
    if (!mounted) return;
    setState(() {
      _busy = false;
      _error = err;
    });
    if (err == null && widget.modal) Navigator.of(context).pop();
  }

  Future<void> _clear() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Clear all keys?'),
        content: const Text('This wipes every saved key and staged card from this device.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Clear', style: TextStyle(color: Pal.danger))),
        ],
      ),
    );
    if (ok != true) return;
    await widget.c.clearVault();
    if (!mounted) return;
    for (final t in [_ak, _as, _gk, _tt, _tc]) {
      t.clear();
    }
    if (widget.modal) Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: const Text('Connect Your Own Accounts'),
          leading: widget.modal ? IconButton(icon: const Icon(Icons.close), onPressed: () => Navigator.pop(context)) : null,
          automaticallyImplyLeading: false,
        ),
        body: SafeArea(
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 520),
              child: ListView(padding: const EdgeInsets.all(16), children: [
                const Text('🛡️ Project Shield v4.4 — paper trading', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
                const SizedBox(height: 6),
                const Text(
                  'Keys are stored only on this device and sent only to Alpaca and Google. '
                  'Shield has no server. Use Alpaca PAPER keys (they start with PK).',
                  style: TextStyle(color: Pal.muted, fontSize: 13),
                ),
                const SizedBox(height: 16),
                SecretField(controller: _ak, label: 'Alpaca API Key ID *', hint: 'PK…'),
                const SizedBox(height: 12),
                SecretField(controller: _as, label: 'Alpaca Secret Key *'),
                const SizedBox(height: 12),
                SecretField(controller: _gk, label: 'Gemini API Key (optional — AI features)'),
                const SizedBox(height: 12),
                SecretField(controller: _tt, label: 'Telegram Bot Token (optional, reserved)'),
                const SizedBox(height: 12),
                TextField(controller: _tc, style: _mono, decoration: const InputDecoration(labelText: 'Telegram Chat ID (optional)')),
                if (_error != null) ...[
                  const SizedBox(height: 12),
                  Container(
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(color: _a(Pal.danger, .15), border: Border.all(color: Pal.danger), borderRadius: BorderRadius.circular(8)),
                    child: Text(_error!, style: const TextStyle(color: Pal.dangerText)),
                  ),
                ],
                const SizedBox(height: 16),
                FilledButton(
                  style: FilledButton.styleFrom(backgroundColor: Pal.emerald, foregroundColor: Colors.black, padding: const EdgeInsets.symmetric(vertical: 14)),
                  onPressed: _busy ? null : _save,
                  child: _busy
                      ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.black))
                      : const Text('Save & Connect', style: TextStyle(fontWeight: FontWeight.w700)),
                ),
                const SizedBox(height: 8),
                TextButton(onPressed: _busy ? null : _clear, child: const Text('Clear all keys', style: TextStyle(color: Pal.danger))),
              ]),
            ),
          ),
        ),
      );
}

// ============================================================================
// WATCH HUD — renders a WatchSnapshot, sends WatchCommands. No keys, no logic.
// ============================================================================

class WatchHudScreen extends StatefulWidget {
  const WatchHudScreen({super.key, required this.link});
  final WatchLink link;
  @override
  State<WatchHudScreen> createState() => _WatchHudScreenState();
}

class _WatchHudScreenState extends State<WatchHudScreen> {
  Timer? _t, _confirmTimer;
  DateTime _now = DateTime.now().toUtc();
  String? _banner;
  bool _bannerBad = false, _confirmAuto = false, _busy = false;

  @override
  void initState() {
    super.initState();
    _t = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() => _now = DateTime.now().toUtc());
    });
    widget.link.addListener(_onLink);
  }

  void _onLink() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _t?.cancel();
    _confirmTimer?.cancel();
    widget.link.removeListener(_onLink);
    super.dispose();
  }

  Future<void> _send(WatchCommand cmd) async {
    setState(() {
      _busy = true;
      _banner = null;
    });
    final ack = await widget.link.send(cmd, DateTime.now().microsecondsSinceEpoch.toString());
    if (!mounted) return;
    setState(() {
      _busy = false;
      if (ack.result == WatchSendResult.phoneOffline) {
        // Never fail silently.
        _banner = '⛔ PHONE OFFLINE – NOT SENT\n${cmd == WatchCommand.killSwitch ? 'Use the PHONE app NOW to flatten.' : 'Mode was NOT changed.'}';
        _bannerBad = true;
        HapticFeedback.heavyImpact();
      } else {
        _banner = '✓ Sent to phone${ack.detail == null ? '' : ' — ${ack.detail}'}';
        _bannerBad = ack.detail?.contains('REFUSED') == true || ack.detail?.contains('refused') == true || ack.detail?.contains('FAILURES') == true;
      }
    });
  }

  void _onModeTap(WatchSnapshot? s) {
    if (s?.mode == 'AUTO') {
      _send(WatchCommand.setModeManual);
    } else if (!_confirmAuto) {
      setState(() => _confirmAuto = true);
      _confirmTimer?.cancel();
      _confirmTimer = Timer(const Duration(seconds: 4), () {
        if (mounted) setState(() => _confirmAuto = false);
      });
    } else {
      _confirmTimer?.cancel();
      setState(() => _confirmAuto = false);
      _send(WatchCommand.setModeAuto);
    }
  }

  @override
  Widget build(BuildContext context) {
    final link = widget.link;
    final s = link.snapshot;
    final offline = !link.phoneReachable || s == null || _now.difference(s.updatedAt) > const Duration(seconds: 30);
    final auto = s?.mode == 'AUTO';
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(10),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Row(mainAxisAlignment: MainAxisAlignment.center, children: [
              Icon(Icons.circle, size: 9, color: offline ? Pal.danger : Pal.emerald),
              const SizedBox(width: 6),
              Text(offline ? 'Phone offline' : 'MYT ${hms(mytTime(_now))}',
                  style: TextStyle(fontSize: 12, color: offline ? Pal.danger : Pal.muted, fontWeight: FontWeight.w700)),
            ]),
            const SizedBox(height: 6),
            const Text('MY ACCOUNT EQUITY', textAlign: TextAlign.center, style: TextStyle(color: Pal.muted, fontSize: 10)),
            FittedBox(
              fit: BoxFit.scaleDown,
              child: Text(s?.equity ?? '—', style: const TextStyle(color: Colors.white, fontSize: 30, fontWeight: FontWeight.w800)),
            ),
            Text(
              s?.pnlText ?? '—',
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 13, color: (s?.pnlPositive ?? true) ? Pal.emerald : Pal.dangerSoft, fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 8),
            FilledButton(
              style: FilledButton.styleFrom(
                backgroundColor: _confirmAuto ? _a(Pal.amber, .3) : (auto ? _a(Pal.emerald, .2) : _a(Pal.sky, .2)),
                foregroundColor: _confirmAuto ? Pal.amber : (auto ? Pal.emerald : Pal.sky),
                padding: const EdgeInsets.symmetric(vertical: 10),
              ),
              onPressed: _busy ? null : () => _onModeTap(s),
              child: Text(
                _confirmAuto ? 'TAP AGAIN TO CONFIRM AUTO' : (auto ? '🤖 AUTO EXECUTION ON\nTap to switch to MANUAL' : '📱 MANUAL PHONE MODE'),
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700),
              ),
            ),
            if ((s?.pendingCount ?? 0) > 0)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: GestureDetector(
                  onTap: HapticFeedback.mediumImpact,
                  child: Text('${s!.pendingCount} pending on phone',
                      textAlign: TextAlign.center, style: const TextStyle(color: Pal.sky, fontSize: 12, fontWeight: FontWeight.w700)),
                ),
              ),
            const SizedBox(height: 8),
            const Text('⚡ INSTANT EXECUTION FEED', style: TextStyle(color: Pal.muted, fontSize: 9.5)),
            const SizedBox(height: 2),
            for (final line in (s?.feedTop3 ?? const <String>[]))
              Text(line, maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontFamily: 'monospace', fontSize: 10, color: Colors.white70)),
            const SizedBox(height: 8),
            FilledButton(
              style: FilledButton.styleFrom(backgroundColor: Pal.danger, foregroundColor: Colors.white, padding: const EdgeInsets.symmetric(vertical: 10)),
              onPressed: _busy
                  ? null
                  : () => setState(() {
                        _banner = 'Press and HOLD to send the kill switch';
                        _bannerBad = false;
                      }),
              onLongPress: _busy
                  ? null
                  : () {
                      HapticFeedback.heavyImpact();
                      _send(WatchCommand.killSwitch);
                    },
              child: const Text('🚨 KILL SWITCH & FLATTEN\n(hold to confirm)',
                  textAlign: TextAlign.center, style: TextStyle(fontSize: 12, fontWeight: FontWeight.w800)),
            ),
            if (_busy) const Padding(padding: EdgeInsets.only(top: 6), child: LinearProgressIndicator(color: Pal.danger)),
            if (_banner != null)
              Container(
                margin: const EdgeInsets.only(top: 8),
                padding: const EdgeInsets.all(8),
                decoration: BoxDecoration(
                  color: _bannerBad ? Pal.danger : _a(Pal.emerald, .2),
                  border: Border.all(color: _bannerBad ? Colors.white : Pal.emerald, width: _bannerBad ? 2 : 1),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Text(_banner!,
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: _bannerBad ? 13 : 11.5, fontWeight: FontWeight.w800, color: Colors.white)),
              ),
          ]),
        ),
      ),
    );
  }
}

class WatchPreviewScreen extends StatelessWidget {
  const WatchPreviewScreen({super.key, required this.link});
  final PreviewWatchLink link;
  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Watch HUD preview')),
        body: ListenableBuilder(
          listenable: link,
          builder: (context, _) => ListView(padding: const EdgeInsets.all(16), children: [
            Center(
              child: Container(
                width: 300,
                height: 380,
                decoration: BoxDecoration(border: Border.all(color: Pal.border, width: 3), borderRadius: BorderRadius.circular(36)),
                clipBehavior: Clip.antiAlias,
                child: WatchHudScreen(link: link),
              ),
            ),
            const SizedBox(height: 16),
            SwitchListTile(
              activeTrackColor: Pal.danger,
              title: const Text('Simulate phone link lost'),
              subtitle: const Text('Then tap the watch kill switch to see the "NOT SENT" state.', style: TextStyle(color: Pal.muted, fontSize: 12)),
              value: !link.phoneReachable,
              onChanged: (v) => link.reachable = !v,
            ),
            const Text(
              'This preview runs in-process. A physical Wear OS watch needs a separate Wear module whose WatchLink '
              'uses the Wear Data Layer (DataClient for snapshots, MessageClient for commands).',
              style: TextStyle(color: Pal.muted, fontSize: 12),
            ),
          ]),
        ),
      );
}


// ============================================================================
// FIELD MANUAL (GUIDE)
// ============================================================================

class FieldManualScreen extends StatelessWidget {
  const FieldManualScreen({super.key});

  Widget _guideSection(String title, IconData icon, Color color, List<Widget> content) {
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(color: Pal.surface, borderRadius: BorderRadius.circular(12), border: Border.all(color: _a(color, 0.5))),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Icon(icon, color: color, size: 22),
          const SizedBox(width: 8),
          Text(title, style: TextStyle(color: color, fontSize: 16, fontWeight: FontWeight.w800)),
        ]),
        const Padding(padding: EdgeInsets.symmetric(vertical: 8), child: Divider()),
        ...content,
      ]),
    );
  }

  Widget _bullet(String title, String desc) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Text('• ', style: TextStyle(color: Pal.emerald, fontSize: 18, fontWeight: FontWeight.w800)),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13, color: Pal.text)),
            const SizedBox(height: 2),
            Text(desc, style: const TextStyle(color: Pal.muted, fontSize: 12, height: 1.4)),
          ]),
        ),
      ]),
    );
  }

  Widget _promptExample(String prompt) {
    return Container(
      margin: const EdgeInsets.only(bottom: 6),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(color: Pal.bg, borderRadius: BorderRadius.circular(6), border: Border.all(color: Pal.border)),
      child: Row(children: [
        const Icon(Icons.chat_bubble_outline, size: 14, color: Pal.sky),
        const SizedBox(width: 8),
        Expanded(child: Text('"$prompt"', style: const TextStyle(fontFamily: 'monospace', fontSize: 12, color: Pal.sky))),
      ]),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('📖 Shield Field Manual', style: TextStyle(fontWeight: FontWeight.w800))),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text('Welcome to Project Shield', style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800)),
          const SizedBox(height: 6),
          const Text(
            'This is not a normal trading app. It is an algorithmic risk cockpit. It prevents you from blowing up your account by strictly enforcing mathematical rules and reading live institutional order flow.',
            style: TextStyle(color: Pal.muted, fontSize: 13, height: 1.5),
          ),
          const SizedBox(height: 20),

          _guideSection('🤖 1. The AI Copilot', Icons.smart_toy, Pal.sky, [
            const Text('The Copilot doesn\'t just chat—it physically controls the app for you. You can ask it to analyze your risk or stage trades.', style: TextStyle(color: Pal.text, fontSize: 12.5)),
            const SizedBox(height: 10),
            _promptExample('How is my risk right now?'),
            _promptExample('Stage a swing trade for NVDA.'),
            _promptExample('Trim my TSLA position.'),
            _promptExample('Are there any bad news for the stocks I own?'),
          ]),

          _guideSection('📐 2. The Risk Math (Why You Cannot YOLO)', Icons.calculate, Pal.emerald, [
            _bullet('1% Risk Budget', 'The app automatically calculates your share size so that if your Stop Loss is hit, you only ever lose exactly 1% of your total account equity.'),
            _bullet('15% Notional Cap', 'Even if a stock has a tight stop loss, the app will never let you put more than 15% of your total money into a single trade.'),
            _bullet('-2.5% Daily Drawdown Halt', 'If you lose 2.5% of your equity in a single day, the system physically locks your account to prevent revenge trading.'),
          ]),

          _guideSection('📰 3. News & Bearish Defense', Icons.newspaper, Pal.amber, [
            const Text('The app streams live Benzinga institutional news and grades it with Gemini AI.', style: TextStyle(color: Pal.text, fontSize: 12.5)),
            const SizedBox(height: 10),
            _bullet('Automated Defense', 'If a severe bearish headline drops for a stock you currently hold in your portfolio, the app spawns a massive red alert card with a 1-tap "Close Position" button.'),
            _bullet('Trap Filter', 'If you ask the AI to trade a stock, but the live news is secretly bearish, the AI will veto the trade to protect you.'),
          ]),

          _guideSection('🚀 4. Staging a Trade', Icons.show_chart, Pal.indigo, [
            _bullet('The +15 bps Limit Collar', 'Shield never uses Market Orders (which suffer massive slippage). It uses a +0.15% limit collar to guarantee you get a fair entry price, or it cancels.'),
            _bullet('Confluence Score', 'Before you approve a card, the app checks the 5-minute chart. It scores the trade from 0-100 based on VWAP, RSI, and Volume Surges.'),
          ]),

          _guideSection('🚨 5. The Emergency Kill Switch', Icons.warning, Pal.dangerSoft, [
            const Text('Located on the Cockpit tab and the Watch HUD. Press and HOLD the red button for 2 seconds to instantly cancel all open orders and market-sell all open positions to cash. Use this during flash crashes.', style: TextStyle(color: Pal.text, fontSize: 12.5, height: 1.4)),
          ]),
          
          const SizedBox(height: 20),
          const Center(child: Text('Built by Project Shield', style: TextStyle(color: Pal.border, fontWeight: FontWeight.w800, fontSize: 12))),
          const SizedBox(height: 20),
        ],
      ),
    );
  }
}
