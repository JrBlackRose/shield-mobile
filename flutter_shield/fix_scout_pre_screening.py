with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

old_prescreen = """      // Pre-screen candidates with the quantitative Pullback Sniper so we never get stuck on BTCUSD alone!
      final passing = <String>[];
      for (final sym in candidates.take(8)) {
        try {
          final isC = isCryptoSymbol(sym);
          final q = isC ? await _fetchCryptoQuote(sym) : await _fetchStockQuote(sym);
          if (q == null || q.ask <= 0) continue;
          final cScore = isC ? await _computeCryptoConf(sym, q.ask) : await _computeConf(sym, q.ask);
          if (cScore != null && cScore.points >= 2) {
            passing.add(sym);
          }
        } catch (_) {}
      }"""

new_prescreen = """      // Pre-screen candidates with the quantitative Pullback Sniper so we never get stuck on BTCUSD alone!
      final passing = <String>[];
      for (final sym in candidates.take(8)) {
        try {
          final qs = await _quoteFor(sym);
          if (qs == null || qs.ask <= 0) continue;
          final cScore = await _computeConfluence(sym, qs.ask, live: _resolveVenue(sym) != VenueKind.closed);
          if (cScore != null && cScore.points >= 2) {
            passing.add(sym);
          }
        } catch (_) {}
      }"""

if old_prescreen in ctrl:
    ctrl = ctrl.replace(old_prescreen, new_prescreen, 1)
    with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
        f.write(ctrl)
    print("[OK] Replaced pre-screening loop with correct _computeConfluence() and _quoteFor() methods.")
else:
    print("[WARN] Could not find exact old_prescreen block.")
