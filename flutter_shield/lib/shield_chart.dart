// ============================================================================
// PROJECT SHIELD v4.3 — lib/shield_chart.dart
//
// Standalone interactive chart card. Depends only on:
//   shield_controller.dart  (ShieldController chart API)
//   shield_core.dart        (Candle, Position, PendingCard, CardStatus, usd...)
//
// Colours are declared locally so this file has no import cycle with main.dart.
// ============================================================================

import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'shield_controller.dart';
import 'shield_core.dart';

abstract final class _C {
  static const bg = Color(0xFF020617);
  static const surface = Color(0xFF0F172A);
  static const border = Color(0xFF1E293B);
  static const up = Color(0xFF10B981);
  static const down = Color(0xFFF87171);
  static const sky = Color(0xFF0EA5E9);
  static const muted = Color(0xFF94A3B8);
  static const text = Color(0xFFE2E8F0);
  static const vwap = Color(0xFFA78BFA);
}

Color _al(Color c, double o) => c.withAlpha((o * 255).round());

const List<String> _kQuick = ['BTC', 'ETH', 'SOL', 'NVDA', 'TSLA', 'AAPL'];
const List<({String label, String value})> _kFrames = [
  (label: '5M', value: '5Min'),
  (label: '15M', value: '15Min'),
  (label: '1H', value: '1Hour'),
  (label: '1D', value: '1Day'),
];

String _px(double v) {
  final a = v.abs();
  if (a >= 1000) return v.toStringAsFixed(2);
  if (a >= 1) return v.toStringAsFixed(2);
  if (a >= 0.01) return v.toStringAsFixed(4);
  return v.toStringAsFixed(6);
}

String _vol(double v) {
  if (v >= 1e9) return '${(v / 1e9).toStringAsFixed(2)}B';
  if (v >= 1e6) return '${(v / 1e6).toStringAsFixed(2)}M';
  if (v >= 1e3) return '${(v / 1e3).toStringAsFixed(1)}K';
  return v.toStringAsFixed(v == v.roundToDouble() ? 0 : 2);
}

String _two(int n) => n.toString().padLeft(2, '0');

/// Safe clamp that returns a double and never throws when hi < lo.
double _cl(double v, double lo, double hi) => hi < lo ? lo : v.clamp(lo, hi).toDouble();

String _stamp(DateTime t, String tf) {
  final l = t.toLocal();
  final d = '${l.year}-${_two(l.month)}-${_two(l.day)}';
  if (tf == '1Day') return d;
  return '$d ${_two(l.hour)}:${_two(l.minute)}';
}

/// Strips crypto quote suffixes so "BTC/USD" and "BTC" compare equal.
String _norm(String s) {
  var u = s.toUpperCase().trim();
  for (final suf in const ['/USD', 'USD']) {
    if (u.endsWith(suf) && u.length > suf.length) {
      u = u.substring(0, u.length - suf.length);
      break;
    }
  }
  return u.replaceAll('/', '');
}

class _Level {
  const _Level(this.label, this.price, this.color);
  final String label;
  final double price;
  final Color color;
}

class ShieldChartCard extends StatefulWidget {
  const ShieldChartCard({super.key, required this.controller});
  final ShieldController controller;

  @override
  State<ShieldChartCard> createState() => _ShieldChartCardState();
}

class _ShieldChartCardState extends State<ShieldChartCard> {
  final TextEditingController _input = TextEditingController();
  final FocusNode _focus = FocusNode();
  bool _candles = true;
  int? _touch; // index of scrubbed candle, null = none
  bool _didInitialLoad = false;

  ShieldController get c => widget.controller;

  @override
  void initState() {
    super.initState();
    _input.text = c.chartSymbol;
    c.addListener(_onCtl);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || _didInitialLoad) return;
      _didInitialLoad = true;
      if (c.chartCandles.isEmpty && !c.chartLoading) {
        c.loadChart(c.chartSymbol.isEmpty ? 'NVDA' : c.chartSymbol);
      }
    });
  }

  @override
  void didUpdateWidget(ShieldChartCard old) {
    super.didUpdateWidget(old);
    if (old.controller != widget.controller) {
      old.controller.removeListener(_onCtl);
      widget.controller.addListener(_onCtl);
    }
  }

  @override
  void dispose() {
    c.removeListener(_onCtl);
    _input.dispose();
    _focus.dispose();
    super.dispose();
  }

  String _lastSymbol = '';
  void _onCtl() {
    if (!mounted) return;
    // Keep the text field in sync when the symbol is changed elsewhere
    // (e.g. the Portfolio "Chart" icon button) — but never clobber typing.
    if (c.chartSymbol != _lastSymbol) {
      _lastSymbol = c.chartSymbol;
      if (!_focus.hasFocus) _input.text = c.chartSymbol;
      _touch = null;
    }
    if (_touch != null && _touch! >= c.chartCandles.length) _touch = null;
    setState(() {});
  }

  void _submit([String? raw]) {
    final s = (raw ?? _input.text).trim().toUpperCase();
    if (s.isEmpty) return;
    _input.text = s;
    _focus.unfocus();
    setState(() => _touch = null);
    c.loadChart(s);
  }

  void _setTf(String tf) {
    if (tf == c.chartTimeframe && c.chartCandles.isNotEmpty) return;
    setState(() => _touch = null);
    c.loadChart(c.chartSymbol, timeframe: tf);
  }

  /// Entry / TP / SL lines for the displayed symbol (staged card or position).
  List<_Level> _levels() {
    final sym = _norm(c.chartSymbol);
    final out = <_Level>[];

    PendingCard? card;
    for (final p in c.pendingCards) {
      if (_norm(p.ticker) == sym && p.status == CardStatus.pending) {
        card = p;
        break;
      }
    }
    if (card != null) {
      if (card.entry > 0) out.add(_Level('ENTRY', card.entry / kPipsPerUsd, _C.text));
      if (card.takeProfit > 0) out.add(_Level('TP', card.takeProfit / kPipsPerUsd, _C.up));
      if (card.stopLoss > 0) out.add(_Level('SL', card.stopLoss / kPipsPerUsd, _C.down));
      return out;
    }

    Position? pos;
    for (final p in c.positions) {
      if (_norm(p.symbol) == sym) {
        pos = p;
        break;
      }
    }
    if (pos != null) {
      if (pos.avgEntry > 0) out.add(_Level('ENTRY', pos.avgEntry / kPipsPerUsd, _C.text));
      // Pull bracket legs from any open order for this symbol: limit leg = TP,
      // stop leg = SL. We read them from the human-readable leg lines so this
      // file does not depend on OrderSummary internals.
      for (final o in c.orders) {
        if (_norm(_orderSymbolGuess(o.line)) != sym) continue;
        for (final l in o.legs) {
          final parsed = _parseLeg(l.line);
          if (parsed != null) out.add(parsed);
        }
      }
    }
    return out;
  }

  static String _orderSymbolGuess(String line) {
    // Lines look like "BUY 10 NVDA @ ..." / "SELL NVDA ..." — find first
    // all-caps token that is not a verb/keyword.
    const skip = {
      'BUY', 'SELL', 'LIMIT', 'STOP', 'MARKET', 'GTC', 'DAY', 'OPEN', 'LONG',
      'SHORT', 'BRACKET', 'OCO', 'OTO', 'TP', 'SL', 'QTY', 'SH', 'NEW',
    };
    for (final m in RegExp(r'[A-Z][A-Z0-9./]{0,9}').allMatches(line)) {
      final t = m.group(0)!;
      if (!skip.contains(t)) return t;
    }
    return '';
  }

  static _Level? _parseLeg(String line) {
    final up = line.toUpperCase();
    final m = RegExp(r'(\d+(?:\.\d+)?)\s*$').firstMatch(line.trim()) ??
        RegExp(r'@\s*\$?(\d+(?:\.\d+)?)').firstMatch(line);
    if (m == null) return null;
    final v = double.tryParse(m.group(1)!);
    if (v == null || v <= 0) return null;
    if (up.contains('STOP')) return _Level('SL', v, _C.down);
    if (up.contains('LIMIT')) return _Level('TP', v, _C.up);
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final candles = c.chartCandles;
    final levels = _levels();
    final last = candles.isEmpty ? null : candles.last;
    final first = candles.isEmpty ? null : candles.first;
    final chg = (last != null && first != null && first.o > 0) ? (last.c - first.o) / first.o * 100 : 0.0;
    final chgColor = chg >= 0 ? _C.up : _C.down;

    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: _C.surface,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: _C.border),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        // ---- title row ----
        Row(children: [
          const Text('📊', style: TextStyle(fontSize: 16)),
          const SizedBox(width: 6),
          Text(c.chartSymbol.isEmpty ? '—' : c.chartSymbol,
              style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: _C.text)),
          const SizedBox(width: 10),
          if (last != null) ...[
            Text(_px(last.c),
                style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15, color: _C.text, fontFamily: 'monospace')),
            const SizedBox(width: 6),
            Text('${chg >= 0 ? '+' : ''}${chg.toStringAsFixed(2)}%',
                style: TextStyle(fontWeight: FontWeight.w700, fontSize: 12.5, color: chgColor)),
          ],
          const Spacer(),
          _ModeToggle(candles: _candles, onChanged: (v) => setState(() => _candles = v)),
        ]),
        const SizedBox(height: 10),

        // ---- ticker search ----
        Row(children: [
          Expanded(
            child: SizedBox(
              height: 40,
              child: TextField(
                controller: _input,
                focusNode: _focus,
                textCapitalization: TextCapitalization.characters,
                textInputAction: TextInputAction.search,
                autocorrect: false,
                enableSuggestions: false,
                onSubmitted: _submit,
                style: const TextStyle(fontFamily: 'monospace', fontSize: 14, color: _C.text),
                inputFormatters: [
                  FilteringTextInputFormatter.allow(RegExp(r'[A-Za-z0-9./-]')),
                  TextInputFormatter.withFunction((o, n) => n.copyWith(text: n.text.toUpperCase())),
                ],
                decoration: InputDecoration(
                  hintText: 'Search ticker (NVDA, BTC/USD)…',
                  prefixIcon: const Icon(Icons.search, size: 18, color: _C.muted),
                  contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                  suffixIcon: _input.text.isEmpty
                      ? null
                      : IconButton(
                          visualDensity: VisualDensity.compact,
                          icon: const Icon(Icons.close, size: 16, color: _C.muted),
                          onPressed: () => setState(_input.clear),
                        ),
                ),
                onChanged: (_) => setState(() {}),
              ),
            ),
          ),
          const SizedBox(width: 8),
          SizedBox(
            height: 40,
            child: FilledButton(
              style: FilledButton.styleFrom(
                backgroundColor: _C.up,
                foregroundColor: Colors.black,
                padding: const EdgeInsets.symmetric(horizontal: 16),
              ),
              onPressed: c.chartLoading ? null : _submit,
              child: const Text('Go', style: TextStyle(fontWeight: FontWeight.w800)),
            ),
          ),
        ]),
        const SizedBox(height: 8),

        // ---- quick chips ----
        SizedBox(
          height: 32,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            itemCount: _kQuick.length,
            separatorBuilder: (_, __) => const SizedBox(width: 6),
            itemBuilder: (_, i) {
              final s = _kQuick[i];
              final sel = _norm(c.chartSymbol) == s;
              return GestureDetector(
                onTap: () => _submit(s),
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: sel ? _al(_C.sky, .2) : _C.bg,
                    borderRadius: BorderRadius.circular(16),
                    border: Border.all(color: sel ? _C.sky : _C.border),
                  ),
                  child: Text(s,
                      style: TextStyle(
                          fontSize: 12, fontWeight: FontWeight.w700, color: sel ? _C.sky : _C.muted)),
                ),
              );
            },
          ),
        ),
        const SizedBox(height: 10),

        // ---- timeframe pills ----
        Row(children: [
          for (final f in _kFrames) ...[
            Expanded(
              child: GestureDetector(
                onTap: () => _setTf(f.value),
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 150),
                  height: 32,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: c.chartTimeframe == f.value ? _al(_C.up, .18) : _C.bg,
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(color: c.chartTimeframe == f.value ? _C.up : _C.border),
                  ),
                  child: Text(f.label,
                      style: TextStyle(
                          fontSize: 12.5,
                          fontWeight: FontWeight.w800,
                          color: c.chartTimeframe == f.value ? _C.up : _C.muted)),
                ),
              ),
            ),
            if (f != _kFrames.last) const SizedBox(width: 6),
          ],
        ]),
        const SizedBox(height: 10),

        // ---- OHLCV readout ----
        _Readout(
          candle: _touch != null && _touch! < candles.length ? candles[_touch!] : null,
          fallback: last,
          timeframe: c.chartTimeframe,
          scrubbing: _touch != null,
          vwap: c.chartVwap,
        ),
        const SizedBox(height: 8),

        // ---- chart ----
        SizedBox(
          height: 300,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(8),
            child: Container(
              color: _C.bg,
              child: Stack(children: [
                Positioned.fill(
                  child: candles.isEmpty
                      ? const SizedBox.shrink()
                      : LayoutBuilder(builder: (context, cons) {
                          return _ChartGestures(
                            count: candles.length,
                            width: cons.maxWidth,
                            onTouch: (i) {
                              if (i != _touch) {
                                HapticFeedback.selectionClick();
                                setState(() => _touch = i);
                              }
                            },
                            onEnd: () => setState(() => _touch = null),
                            child: CustomPaint(
                              size: Size(cons.maxWidth, cons.maxHeight),
                              painter: _ChartPainter(
                                candles: candles,
                                candleMode: _candles,
                                vwap: c.chartVwap,
                                touch: _touch,
                                levels: levels,
                                timeframe: c.chartTimeframe,
                              ),
                            ),
                          );
                        }),
                ),
                if (c.chartLoading)
                  Positioned.fill(
                    child: ColoredBox(
                      color: _al(_C.bg, candles.isEmpty ? 1 : .6),
                      child: const Center(
                        child: SizedBox(
                            width: 26, height: 26, child: CircularProgressIndicator(strokeWidth: 2.5, color: _C.up)),
                      ),
                    ),
                  ),
                if (!c.chartLoading && c.chartError != null)
                  Positioned.fill(
                    child: Center(
                      child: Padding(
                        padding: const EdgeInsets.all(16),
                        child: Column(mainAxisSize: MainAxisSize.min, children: [
                          const Icon(Icons.error_outline, color: _C.down, size: 28),
                          const SizedBox(height: 6),
                          Text(c.chartError!,
                              textAlign: TextAlign.center,
                              style: const TextStyle(color: _C.down, fontSize: 12.5)),
                          const SizedBox(height: 8),
                          OutlinedButton(
                            onPressed: () => c.loadChart(c.chartSymbol),
                            child: const Text('Retry'),
                          ),
                        ]),
                      ),
                    ),
                  ),
                if (!c.chartLoading && c.chartError == null && candles.isEmpty)
                  const Positioned.fill(
                    child: Center(
                      child: Text('No data — search a ticker above.',
                          style: TextStyle(color: _C.muted, fontSize: 12.5)),
                    ),
                  ),
              ]),
            ),
          ),
        ),
        const SizedBox(height: 8),

        // ---- legend ----
        Wrap(spacing: 14, runSpacing: 4, children: [
          if (c.chartVwap != null) const _LegendDot('VWAP', _C.vwap, dashed: true),
          for (final l in levels) _LegendDot('${l.label} ${_px(l.price)}', l.color, dashed: true),
          if (candles.isNotEmpty)
            Text('${candles.length} bars · drag to scrub',
                style: const TextStyle(color: _C.muted, fontSize: 11)),
        ]),
      ]),
    );
  }
}

// ----------------------------------------------------------------------------
// Small widgets
// ----------------------------------------------------------------------------

class _ModeToggle extends StatelessWidget {
  const _ModeToggle({required this.candles, required this.onChanged});
  final bool candles;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    Widget seg(IconData icon, String tip, bool value) {
      final sel = candles == value;
      return Tooltip(
        message: tip,
        child: GestureDetector(
          onTap: () => onChanged(value),
          child: Container(
            width: 36,
            height: 30,
            decoration: BoxDecoration(
              color: sel ? _al(_C.sky, .22) : Colors.transparent,
              borderRadius: BorderRadius.circular(6),
            ),
            child: Icon(icon, size: 18, color: sel ? _C.sky : _C.muted),
          ),
        ),
      );
    }

    return Container(
      padding: const EdgeInsets.all(2),
      decoration: BoxDecoration(
        color: _C.bg,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: _C.border),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        seg(Icons.candlestick_chart_outlined, 'Candles', true),
        seg(Icons.show_chart, 'Line', false),
      ]),
    );
  }
}

class _LegendDot extends StatelessWidget {
  const _LegendDot(this.text, this.color, {this.dashed = false});
  final String text;
  final Color color;
  final bool dashed;

  @override
  Widget build(BuildContext context) => Row(mainAxisSize: MainAxisSize.min, children: [
        SizedBox(
          width: 16,
          height: 6,
          child: CustomPaint(painter: _DashSwatch(color, dashed)),
        ),
        const SizedBox(width: 4),
        Text(text, style: TextStyle(color: color, fontSize: 11, fontWeight: FontWeight.w700)),
      ]);
}

class _DashSwatch extends CustomPainter {
  _DashSwatch(this.color, this.dashed);
  final Color color;
  final bool dashed;
  @override
  void paint(Canvas canvas, Size size) {
    final p = Paint()
      ..color = color
      ..strokeWidth = 1.6;
    if (!dashed) {
      canvas.drawLine(Offset(0, size.height / 2), Offset(size.width, size.height / 2), p);
      return;
    }
    canvas.drawLine(Offset(0, size.height / 2), Offset(5, size.height / 2), p);
    canvas.drawLine(Offset(9, size.height / 2), Offset(size.width, size.height / 2), p);
  }

  @override
  bool shouldRepaint(_DashSwatch o) => o.color != color || o.dashed != dashed;
}

class _Readout extends StatelessWidget {
  const _Readout({
    required this.candle,
    required this.fallback,
    required this.timeframe,
    required this.scrubbing,
    required this.vwap,
  });
  final Candle? candle;
  final Candle? fallback;
  final String timeframe;
  final bool scrubbing;
  final double? vwap;

  @override
  Widget build(BuildContext context) {
    final k = candle ?? fallback;
    if (k == null) {
      return const SizedBox(
        height: 38,
        child: Align(
          alignment: Alignment.centerLeft,
          child: Text('OHLCV —', style: TextStyle(color: _C.muted, fontSize: 12)),
        ),
      );
    }
    final up = k.c >= k.o;
    final tone = up ? _C.up : _C.down;

    Widget cell(String l, String v, [Color? col]) => Padding(
          padding: const EdgeInsets.only(right: 12),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
            Text(l, style: const TextStyle(color: _C.muted, fontSize: 10, fontWeight: FontWeight.w700)),
            Text(v,
                style: TextStyle(
                    color: col ?? _C.text, fontSize: 12, fontWeight: FontWeight.w700, fontFamily: 'monospace')),
          ]),
        );

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: scrubbing ? _al(tone, .08) : _C.bg,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: scrubbing ? _al(tone, .6) : _C.border),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(
          scrubbing ? _stamp(k.t, timeframe) : 'Latest · ${_stamp(k.t, timeframe)}',
          style: TextStyle(color: scrubbing ? tone : _C.muted, fontSize: 11, fontWeight: FontWeight.w700),
        ),
        const SizedBox(height: 3),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(children: [
            cell('O', _px(k.o)),
            cell('H', _px(k.h)),
            cell('L', _px(k.l)),
            cell('C', _px(k.c), tone),
            cell('VOL', _vol(k.v)),
            if (k.vw != null) cell('VW', _px(k.vw!), _C.vwap),
          ]),
        ),
      ]),
    );
  }
}

// ----------------------------------------------------------------------------
// Gesture layer: maps horizontal touch position -> candle index.
// ----------------------------------------------------------------------------

class _ChartGestures extends StatelessWidget {
  const _ChartGestures({
    required this.count,
    required this.width,
    required this.onTouch,
    required this.onEnd,
    required this.child,
  });
  final int count;
  final double width;
  final ValueChanged<int> onTouch;
  final VoidCallback onEnd;
  final Widget child;

  int _index(double dx) {
    final plotW = math.max(1.0, width - _ChartPainter.rightGutter);
    final step = plotW / count;
    return (dx / step).floor().clamp(0, count - 1).toInt();
  }

  @override
  Widget build(BuildContext context) => GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTapDown: (d) => onTouch(_index(d.localPosition.dx)),
        onTapUp: (_) => onEnd(),
        onTapCancel: onEnd,
        onHorizontalDragStart: (d) => onTouch(_index(d.localPosition.dx)),
        onHorizontalDragUpdate: (d) => onTouch(_index(d.localPosition.dx)),
        onHorizontalDragEnd: (_) => onEnd(),
        onHorizontalDragCancel: onEnd,
        onLongPressStart: (d) => onTouch(_index(d.localPosition.dx)),
        onLongPressMoveUpdate: (d) => onTouch(_index(d.localPosition.dx)),
        onLongPressEnd: (_) => onEnd(),
        child: child,
      );
}

// ----------------------------------------------------------------------------
// The painter
// ----------------------------------------------------------------------------

class _ChartPainter extends CustomPainter {
  _ChartPainter({
    required this.candles,
    required this.candleMode,
    required this.vwap,
    required this.touch,
    required this.levels,
    required this.timeframe,
  });

  final List<Candle> candles;
  final bool candleMode;
  final double? vwap;
  final int? touch;
  final List<_Level> levels;
  final String timeframe;

  static const double rightGutter = 54; // price axis labels
  static const double bottomGutter = 16; // time labels
  static const double volFrac = .18; // fraction of plot height used by volume
  static const double topPad = 14;

  TextPainter _tp(String s, Color c, {double size = 10, FontWeight w = FontWeight.w600, bool mono = true}) {
    return TextPainter(
      text: TextSpan(
        text: s,
        style: TextStyle(color: c, fontSize: size, fontWeight: w, fontFamily: mono ? 'monospace' : null),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
  }

  void _dashedH(Canvas canvas, double y, double x0, double x1, Paint p, {double dash = 5, double gap = 4}) {
    var x = x0;
    while (x < x1) {
      canvas.drawLine(Offset(x, y), Offset(math.min(x + dash, x1), y), p);
      x += dash + gap;
    }
  }

  void _dashedPath(Canvas canvas, List<Offset> pts, Paint p, {double dash = 5, double gap = 4}) {
    // Dashed polyline (used for VWAP, which varies per candle).
    var drawing = true;
    var remaining = dash;
    for (var i = 0; i < pts.length - 1; i++) {
      var a = pts[i];
      final b = pts[i + 1];
      var segLen = (b - a).distance;
      if (segLen == 0) continue;
      final dir = (b - a) / segLen;
      while (segLen > 0) {
        final step = math.min(remaining, segLen);
        final end = a + dir * step;
        if (drawing) canvas.drawLine(a, end, p);
        a = end;
        segLen -= step;
        remaining -= step;
        if (remaining <= 0) {
          drawing = !drawing;
          remaining = drawing ? dash : gap;
        }
      }
    }
  }

  @override
  void paint(Canvas canvas, Size size) {
    if (candles.isEmpty || size.width <= rightGutter + 10 || size.height <= 60) return;

    final plotW = size.width - rightGutter;
    final plotH = size.height - bottomGutter - topPad;
    final volH = plotH * volFrac;
    final priceH = plotH - volH - 6;
    final priceTop = topPad;
    final priceBot = priceTop + priceH;
    final volBot = priceTop + plotH;
    final n = candles.length;
    final step = plotW / n;
    final bodyW = math.max(1.0, step * .68);

    // ---- price range (include VWAP + overlay levels that are near the range) ----
    var hi = candles.first.h;
    var lo = candles.first.l;
    var maxVol = 0.0;
    for (final k in candles) {
      if (k.h > hi) hi = k.h;
      if (k.l < lo) lo = k.l;
      if (k.v > maxVol) maxVol = k.v;
    }
    final dataSpan = math.max(hi - lo, hi.abs() * 1e-6);
    final extras = <double>[
      if (vwap != null) vwap!,
      for (final l in levels) l.price,
    ];
    for (final e in extras) {
      // Only stretch the axis for levels within 1.0× the data span so one far-off
      // stop does not flatten the candles.
      if (e > hi && e - hi <= dataSpan) hi = e;
      if (e < lo && lo - e <= dataSpan) lo = e;
    }
    final dataHi = candles.map((k) => k.h).reduce(math.max);
    final dataLo = candles.map((k) => k.l).reduce(math.min);
    var span = hi - lo;
    if (span <= 0) span = math.max(hi.abs() * .01, 0.01);
    final pad = span * .06;
    final top = hi + pad;
    final bot = lo - pad;
    final rng = top - bot;

    double yP(double p) => priceTop + (top - p) / rng * priceH;
    double xC(int i) => (i + .5) * step;

    // ---- grid + right axis labels ----
    final grid = Paint()
      ..color = _al(_C.border, .9)
      ..strokeWidth = 1;
    const rows = 4;
    for (var i = 0; i <= rows; i++) {
      final y = priceTop + priceH * i / rows;
      canvas.drawLine(Offset(0, y), Offset(plotW, y), grid);
      final price = top - rng * i / rows;
      final t = _tp(_px(price), _C.muted, size: 9.5);
      t.paint(canvas, Offset(plotW + 4, _cl(y - t.height / 2, 0.0, size.height - t.height)));
    }

    // ---- time labels ----
    const labelCount = 4;
    for (var i = 0; i < labelCount; i++) {
      final idx = ((n - 1) * i / (labelCount - 1)).round();
      final l = candles[idx].t.toLocal();
      final txt = timeframe == '1Day' ? '${_two(l.month)}/${_two(l.day)}' : '${_two(l.hour)}:${_two(l.minute)}';
      final t = _tp(txt, _C.muted, size: 9.5);
      final x = _cl(xC(idx) - t.width / 2, 0.0, plotW - t.width);
      t.paint(canvas, Offset(x, size.height - bottomGutter + 3));
    }

    // ---- volume bars ----
    for (var i = 0; i < n; i++) {
      final k = candles[i];
      final h = maxVol > 0 ? k.v / maxVol * volH : 0.0;
      final col = k.c >= k.o ? _C.up : _C.down;
      canvas.drawRect(
        Rect.fromLTWH(xC(i) - bodyW / 2, volBot - h, bodyW, math.max(h, 0.5)),
        Paint()..color = _al(col, .35),
      );
    }

    // ---- price series ----
    canvas.save();
    canvas.clipRect(Rect.fromLTWH(0, priceTop - 2, plotW, priceH + 4));

    if (candleMode) {
      for (var i = 0; i < n; i++) {
        final k = candles[i];
        final col = k.c >= k.o ? _C.up : _C.down;
        final x = xC(i);
        final wick = Paint()
          ..color = col
          ..strokeWidth = math.max(1.0, math.min(1.5, bodyW * .2));
        canvas.drawLine(Offset(x, yP(k.h)), Offset(x, yP(k.l)), wick);
        final yo = yP(k.o);
        final yc = yP(k.c);
        final r = Rect.fromLTRB(x - bodyW / 2, math.min(yo, yc), x + bodyW / 2, math.max(yo, yc));
        canvas.drawRect(
          r.height < 1 ? Rect.fromLTWH(r.left, r.top - .5, r.width, 1) : r,
          Paint()..color = col,
        );
      }
    } else {
      final pts = [for (var i = 0; i < n; i++) Offset(xC(i), yP(candles[i].c))];
      final line = Path()..moveTo(pts.first.dx, pts.first.dy);
      for (var i = 1; i < pts.length; i++) {
        line.lineTo(pts[i].dx, pts[i].dy);
      }
      final area = Path.from(line)
        ..lineTo(pts.last.dx, priceBot)
        ..lineTo(pts.first.dx, priceBot)
        ..close();
      final up = candles.last.c >= candles.first.o;
      final col = up ? _C.up : _C.down;
      canvas.drawPath(
        area,
        Paint()
          ..shader = LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [_al(col, .35), _al(col, 0)],
          ).createShader(Rect.fromLTWH(0, priceTop, plotW, priceH)),
      );
      canvas.drawPath(
        line,
        Paint()
          ..color = col
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2
          ..strokeJoin = StrokeJoin.round,
      );
    }

    // ---- VWAP dashed overlay ----
    final vwPts = <Offset>[];
    var havePerBar = false;
    for (var i = 0; i < n; i++) {
      final v = candles[i].vw;
      if (v != null && v > 0) {
        havePerBar = true;
        vwPts.add(Offset(xC(i), yP(v)));
      }
    }
    final vPaint = Paint()
      ..color = _C.vwap
      ..strokeWidth = 1.4
      ..style = PaintingStyle.stroke;
    if (havePerBar && vwPts.length >= 2) {
      _dashedPath(canvas, vwPts, vPaint);
    } else if (vwap != null) {
      _dashedH(canvas, yP(vwap!), 0, plotW, vPaint);
    }
    canvas.restore();

    // ---- high / low labels (on the actual extreme candle) ----
    var hiIdx = 0, loIdx = 0;
    for (var i = 0; i < n; i++) {
      if (candles[i].h >= candles[hiIdx].h) hiIdx = i;
      if (candles[i].l <= candles[loIdx].l) loIdx = i;
    }
    void extremeLabel(String txt, int idx, double price, Color col, bool above) {
      final t = _tp(txt, col, size: 10, w: FontWeight.w800);
      final cx = xC(idx);
      var x = cx - t.width / 2;
      x = _cl(x, 2.0, plotW - t.width - 2);
      final y = above ? yP(price) - t.height - 3 : yP(price) + 3;
      t.paint(canvas, Offset(x, _cl(y, 0.0, size.height - bottomGutter - t.height)));
    }

    extremeLabel('H ${_px(dataHi)}', hiIdx, dataHi, _C.up, true);
    extremeLabel('L ${_px(dataLo)}', loIdx, dataLo, _C.down, false);

    // ---- Entry / TP / SL overlay ----
    for (final l in levels) {
      if (l.price < bot || l.price > top) {
        // Off-screen: pin an arrow label at the edge so the user knows it exists.
        final above = l.price > top;
        final t = _tp('${above ? '▲' : '▼'} ${l.label} ${_px(l.price)}', l.color, size: 9.5, w: FontWeight.w800);
        final y = above ? priceTop + 2 : priceBot - t.height - 2;
        t.paint(canvas, Offset(plotW - t.width - 4, y));
        continue;
      }
      final y = yP(l.price);
      _dashedH(
        canvas,
        y,
        0,
        plotW,
        Paint()
          ..color = l.color
          ..strokeWidth = 1.3,
        dash: 7,
        gap: 4,
      );
      final tag = _tp('${l.label} ${_px(l.price)}', l.color == _C.text ? Colors.black : Colors.white, size: 9.5, w: FontWeight.w800);
      final r = RRect.fromRectAndRadius(
        Rect.fromLTWH(plotW + 1, y - 8, rightGutter - 2, 16),
        const Radius.circular(3),
      );
      canvas.drawRRect(r, Paint()..color = l.color);
      canvas.save();
      canvas.clipRRect(r);
      tag.paint(canvas, Offset(plotW + 3, y - tag.height / 2));
      canvas.restore();
    }

    // ---- last price marker ----
    final lastC = candles.last;
    final lastY = _cl(yP(lastC.c), priceTop, priceBot);
    final lastCol = lastC.c >= lastC.o ? _C.up : _C.down;
    final hasLevelNearLast = levels.any((l) => (yP(l.price) - lastY).abs() < 14);
    if (!hasLevelNearLast) {
      _dashedH(canvas, lastY, 0, plotW, Paint()
        ..color = _al(lastCol, .5)
        ..strokeWidth = 1, dash: 2, gap: 3);
      final t = _tp(_px(lastC.c), Colors.black, size: 9.5, w: FontWeight.w800);
      final r = RRect.fromRectAndRadius(
        Rect.fromLTWH(plotW + 1, lastY - 8, rightGutter - 2, 16),
        const Radius.circular(3),
      );
      canvas.drawRRect(r, Paint()..color = lastCol);
      canvas.save();
      canvas.clipRRect(r);
      t.paint(canvas, Offset(plotW + 3, lastY - t.height / 2));
      canvas.restore();
    }

    // ---- crosshair ----
    final ti = touch;
    if (ti != null && ti >= 0 && ti < n) {
      final k = candles[ti];
      final x = xC(ti);
      final y = _cl(yP(k.c), priceTop, priceBot);
      final cross = Paint()
        ..color = _al(_C.text, .75)
        ..strokeWidth = 1;
      // vertical
      var yy = topPad;
      while (yy < volBot) {
        canvas.drawLine(Offset(x, yy), Offset(x, math.min(yy + 4, volBot)), cross);
        yy += 7;
      }
      // horizontal
      _dashedH(canvas, y, 0, plotW, cross, dash: 4, gap: 3);
      canvas.drawCircle(Offset(x, y), 4, Paint()..color = _C.text);
      canvas.drawCircle(
        Offset(x, y),
        4,
        Paint()
          ..color = _C.bg
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1.5,
      );
      // price tag on the axis
      final pt = _tp(_px(k.c), Colors.black, size: 9.5, w: FontWeight.w800);
      final pr = RRect.fromRectAndRadius(
        Rect.fromLTWH(plotW + 1, y - 8, rightGutter - 2, 16),
        const Radius.circular(3),
      );
      canvas.drawRRect(pr, Paint()..color = _C.text);
      canvas.save();
      canvas.clipRRect(pr);
      pt.paint(canvas, Offset(plotW + 3, y - pt.height / 2));
      canvas.restore();
      // time tag at the bottom
      final tt = _tp(_stamp(k.t, timeframe), Colors.black, size: 9.5, w: FontWeight.w800);
      final tx = _cl(x - tt.width / 2 - 4, 0.0, plotW - tt.width - 8);
      canvas.drawRRect(
        RRect.fromRectAndRadius(
          Rect.fromLTWH(tx, size.height - bottomGutter, tt.width + 8, bottomGutter - 1),
          const Radius.circular(3),
        ),
        Paint()..color = _C.text,
      );
      tt.paint(canvas, Offset(tx + 4, size.height - bottomGutter + (bottomGutter - 1 - tt.height) / 2));
    }
  }

  @override
  bool shouldRepaint(_ChartPainter o) =>
      o.candles != candles ||
      o.candles.length != candles.length ||
      (candles.isNotEmpty && o.candles.isNotEmpty && o.candles.last != candles.last) ||
      o.candleMode != candleMode ||
      o.vwap != vwap ||
      o.touch != touch ||
      o.timeframe != timeframe ||
      !_sameLevels(o.levels, levels);

  static bool _sameLevels(List<_Level> a, List<_Level> b) {
    if (a.length != b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (a[i].price != b[i].price || a[i].label != b[i].label) return false;
    }
    return true;
  }
}
