import re

def clean_nbsp(text):
    return text.replace('\u00a0', ' ')

# ==============================================================================
# 1. PATCH patch_android.py
#    - Override onPause/onStop/onUserLeaveHint with appIsResumed() so Dart timers NEVER pause
#    - Add 0-volume silent AudioTrack loop inside ShieldForegroundService so Vivo
#      FuntouchOS never suspends the app after a few minutes in background
# ==============================================================================
with open('patch_android.py', 'r', encoding='utf-8') as f:
    pa = clean_nbsp(f.read())

if "import android.media.AudioTrack" not in pa:
    pa = pa.replace(
        "import android.net.wifi.WifiManager",
        "import android.media.AudioAttributes\nimport android.media.AudioFormat\nimport android.media.AudioTrack\nimport android.net.wifi.WifiManager"
    )

if "silentTrack: AudioTrack?" not in pa:
    pa = re.sub(
        r"(private var wifiLock:\s*WifiManager\.WifiLock\?\s*=\s*null)",
        r"\1\n    private var silentTrack: AudioTrack? = null",
        pa,
        count=1
    )

audio_init = clean_nbsp("""            wifiLock = wm.createWifiLock(lockMode, "Shield::WifiWakeLock").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (_: Exception) {}
        try {
            val sampleRate = 8000
            val bufSize = AudioTrack.getMinBufferSize(
                sampleRate,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            ).coerceAtLeast(sampleRate * 2)
            val attrs = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build()
            val fmt = AudioFormat.Builder()
                .setSampleRate(sampleRate)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .build()
            silentTrack = AudioTrack(attrs, fmt, bufSize, AudioTrack.MODE_STATIC, 0).apply {
                val silence = ByteArray(bufSize)
                write(silence, 0, silence.size)
                setLoopPoints(0, bufSize / 2, -1)
                setVolume(0.0f)
                play()
            }
        } catch (_: Exception) {}""")

if "silentTrack = AudioTrack" not in pa:
    pa = re.sub(
        r'wifiLock = wm\.createWifiLock\(lockMode, "Shield::WifiWakeLock"\)\.apply \{\s*setReferenceCounted\(false\)\s*acquire\(\)\s*\}\s*\} catch \(_: Exception\) \{\}',
        audio_init,
        pa,
        count=1
    )

if "silentTrack?.stop()" not in pa:
    pa = re.sub(
        r"(if \(wakeLock\?\.isHeld == true\) wakeLock\?\.release\(\))",
        r"try { silentTrack?.stop(); silentTrack?.release() } catch (_: Exception) {}\n            \1",
        pa,
        count=1
    )

resumed_hook = clean_nbsp("""    private val bgRunnable = object : Runnable {
        override fun run() {
            try {
                flutterEngine?.lifecycleChannel?.appIsResumed()
                methodChannel?.invokeMethod("nativeTick", null)
            } catch (_: Exception) {}
            bgHandler.postDelayed(this, 12000L)
        }
    }

    override fun onPause() {
        super.onPause()
        try { flutterEngine?.lifecycleChannel?.appIsResumed() } catch (_: Exception) {}
    }

    override fun onStop() {
        super.onStop()
        try { flutterEngine?.lifecycleChannel?.appIsResumed() } catch (_: Exception) {}
    }

    override fun onUserLeaveHint() {
        super.onUserLeaveHint()
        try { flutterEngine?.lifecycleChannel?.appIsResumed() } catch (_: Exception) {}
    }""")

if "appIsResumed()" not in pa:
    pa = re.sub(
        r'private val bgRunnable = object : Runnable \{\s*override fun run\(\) \{\s*try \{\s*methodChannel\?\.invokeMethod\("nativeTick", null\)\s*\} catch \(_: Exception\) \{\}\s*bgHandler\.postDelayed\(this, 25000L\)\s*\}\s*\}',
        resumed_hook,
        pa,
        count=1
    )

with open('patch_android.py', 'w', encoding='utf-8') as f:
    f.write(pa)
print("[OK] Patched patch_android.py (appIsResumed override + Silent AudioTrack + 12s Heartbeat)")

# ==============================================================================
# 2. PATCH lib/shield_core.dart
#    - Remove 24/7 Crypto from AUTO scan universe (stops BTC/ETH/UNI/SOL bleed!)
#    - Add high-momentum US stocks + Inverse Short ETFs (SQQQ, SOXS, SPXS, TSLQ, NVDS)
#    - Set quick-scalp RiskRules (-0.55% SL / +0.45% TP)
# ==============================================================================
with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = clean_nbsp(f.read())

core = re.sub(
    r"static const int swingStopBps = \d+;\s*static const int swingTargetBps = \d+;\s*static const int intradayStopBps = \d+;\s*static const int intradayTargetBps = \d+;",
    "static const int swingStopBps = 95;\n  static const int swingTargetBps = 90;\n  static const int intradayStopBps = 55;\n  static const int intradayTargetBps = 45;",
    core
)

new_equities = clean_nbsp("""const List<String> kLiquidUsEquities = [
  'NVDA', 'TSLA', 'AMD', 'PLTR', 'META', 'AMZN', 'NFLX', 'MSFT', 'AAPL', 'GOOGL',
  'AVGO', 'MU', 'SMCI', 'ARM', 'COIN', 'HOOD', 'MSTR', 'SOFI', 'HIMS', 'APP',
  'CRWD', 'PANW', 'SNOW', 'SHOP', 'UBER', 'ORCL', 'CRM', 'QCOM', 'TSM', 'LLY',
  // Inverse Short ETFs (let the bot profit when the market/tech dumps!)
  'SQQQ', 'SOXS', 'SPXS', 'TSLQ', 'NVDS',
];""")

core = re.sub(
    r"const List<String> kLiquidUsEquities = \[.*?\];",
    new_equities,
    core,
    count=1,
    flags=re.DOTALL
)

core = core.replace("out.addAll(kCryptoScanPairs);", "// Crypto disabled in AUTO scan universe")

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart (Crypto removed from AUTO, Inverse Short ETFs added, Quick-Scalp brackets set)")

# ==============================================================================
# 3. PATCH lib/shield_crypto_guardian.dart
#    - Cancel open bracket orders BEFORE calling closePosition(sym) so Alpaca never blocks stock exits!
#    - Add Somes Quick-Scalp (+$22 instant bank), +$9.50 Break-Even Shield, and 10m/18m Timeline Exit
# ==============================================================================
with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cg = clean_nbsp(f.read())

if "_entrySeenMs" not in cg:
    cg = cg.replace(
        "final Map<String, int> _stockLossBreach = <String, int>{};",
        "final Map<String, int> _stockLossBreach = <String, int>{};\n  final Map<String, int> _entrySeenMs = <String, int>{};"
    )

new_somes_fn = clean_nbsp("""Future<({bool ok, String message})> _forceCloseStockOrCrypto(_AlpacaExtras ex, String sym) async {
    for (final o in c._rawOrders) {
      final oSym = normSym('${o['symbol'] ?? ''}');
      final st = '${o['status'] ?? ''}'.toLowerCase();
      if (oSym == sym && st != 'canceled' && st != 'filled' && st != 'expired' && st != 'rejected') {
        try {
          await ex.cancelOrderById('${o['id']}');
        } catch (_) {}
      }
    }
    await Future<void>.delayed(const Duration(milliseconds: 650));
    return c.closePosition(sym);
  }

  Future<void> _runSomesAutoCashout() async {
    final ex = c._extras();
    final nowMs = DateTime.now().toUtc().millisecondsSinceEpoch;
    final activeSyms = <String>{};
    var peaksDirty = false;

    for (final p in c.positions) {
      if (!p.isLong || p.avgEntry <= 0 || p.currentPrice <= 0 || p.qtyDouble <= 0) continue;
      final sym = normSym(p.symbol);
      activeSyms.add(sym);
      if (c._closing.contains(sym)) continue;
      final isCrypto = isCryptoSymbol(sym);
      if (!isCrypto && !c.sessionOpen) continue;

      final firstMs = _entrySeenMs.putIfAbsent(sym, () => nowMs);
      final ageMin = (nowMs - firstMs) / 60000.0;

      final plUsd = p.unrealizedPl / kPipsPerUsd;
      final gainBps = ((p.currentPrice - p.avgEntry) * kBpsDenom) ~/ p.avgEntry;

      final prevPeakUsd = _peakPlUsd[sym] ?? plUsd;
      final prevPeakBps = _peakGainBps[sym] ?? gainBps;
      final peakUsd = math.max(prevPeakUsd, plUsd);
      final peakBps = math.max(prevPeakBps, gainBps);
      if (peakUsd > prevPeakUsd || peakBps > prevPeakBps || !_peakPlUsd.containsKey(sym)) {
        _peakPlUsd[sym] = peakUsd;
        _peakGainBps[sym] = peakBps;
        peaksDirty = true;
      }

      // 1. Somes Quick-Scalp Cashout (+$22 USD or +0.25%): Take quick profit immediately!
      if (plUsd >= 22.0 || gainBps >= 25) {
        c._log('💰 QUICK-SCALP CASHOUT $sym: Banked +\\$${plUsd.toStringAsFixed(2)} (+${(gainBps / 100).toStringAsFixed(2)}%) immediately!', level: 'ok');
        final res = await _forceCloseStockOrCrypto(ex, sym);
        unawaited(c._notifyOS('💰 Quick Scalp Banked: $sym (+\\$${plUsd.toStringAsFixed(2)})', res.message));
        _peakPlUsd.remove(sym);
        _peakGainBps.remove(sym);
        _entrySeenMs.remove(sym);
        peaksDirty = true;
        continue;
      }

      // 2. Peak-Pullback Lock (Peaked >= +$13, pulled back 30% while still >= +$5):
      if ((peakUsd >= 13.0 || peakBps >= 14) && plUsd >= 5.0 && plUsd <= peakUsd * 0.70) {
        c._log('💰 PROFIT-LOCK $sym: Peaked at +\\$${peakUsd.toStringAsFixed(2)}, banked +\\$${plUsd.toStringAsFixed(2)} on pullback!', level: 'ok');
        final res = await _forceCloseStockOrCrypto(ex, sym);
        unawaited(c._notifyOS('💰 Profit Locked: $sym (+\\$${plUsd.toStringAsFixed(2)})', res.message));
        _peakPlUsd.remove(sym);
        _peakGainBps.remove(sym);
        _entrySeenMs.remove(sym);
        peaksDirty = true;
        continue;
      }

      // 3. Never-Go-Red Break-Even Shield (Peaked >= +$9.50, faded to <= +$2.50):
      if ((peakUsd >= 9.50 || peakBps >= 11) && plUsd <= 2.50) {
        c._log('🛡️ BREAK-EVEN SHIELD $sym: Peaked at +\\$${peakUsd.toStringAsFixed(2)}, exited at +\\$${plUsd.toStringAsFixed(2)} before turning red!', level: 'ok');
        final res = await _forceCloseStockOrCrypto(ex, sym);
        unawaited(c._notifyOS('🛡️ Break-Even Saved: $sym', res.message));
        _peakPlUsd.remove(sym);
        _peakGainBps.remove(sym);
        _entrySeenMs.remove(sym);
        peaksDirty = true;
        continue;
      }

      // 4. Somes Timeline Rule (Audio 3):
      //    - After 10 mins: if green (>= +$4.50), bank it immediately so it doesn't decay!
      //    - After 18 mins: if stagnant (between -$28 and +$4.50), exit stale trade before it fails!
      if (ageMin >= 10.0 && plUsd >= 4.50) {
        c._log('⏱️ TIMELINE PROFIT EXIT $sym: Open ${ageMin.toStringAsFixed(0)}m, banking +\\$${plUsd.toStringAsFixed(2)} before momentum fades!', level: 'ok');
        final res = await _forceCloseStockOrCrypto(ex, sym);
        unawaited(c._notifyOS('⏱️ Timeline Profit Banked: $sym (+\\$${plUsd.toStringAsFixed(2)})', res.message));
        _peakPlUsd.remove(sym);
        _peakGainBps.remove(sym);
        _entrySeenMs.remove(sym);
        peaksDirty = true;
        continue;
      }
      if (ageMin >= 18.0 && plUsd >= -28.0 && plUsd < 4.50) {
        c._log('⏱️ TIMELINE STALE EXIT $sym: Open ${ageMin.toStringAsFixed(0)}m with no breakout (\\$${plUsd.toStringAsFixed(2)}) — scratching trade!', level: 'warn');
        final res = await _forceCloseStockOrCrypto(ex, sym);
        unawaited(c._notifyOS('⏱️ Stale Trade Scratched: $sym', res.message));
        _peakPlUsd.remove(sym);
        _peakGainBps.remove(sym);
        _entrySeenMs.remove(sym);
        peaksDirty = true;
        continue;
      }

      // 5. Tight Stop-Loss Guard (-0.52% or -$48 USD)
      if (gainBps <= -52 || plUsd <= -48.0) {
        final n = (_stockLossBreach[sym] ?? 0) + 1;
        _stockLossBreach[sym] = n;
        if (n >= 2) {
          c._log('🚨 TIGHT STOP-LOSS $sym: Cut loss at \\$${plUsd.toStringAsFixed(2)} (${(gainBps / 100).toStringAsFixed(2)}%) to protect capital.', level: 'error');
          final res = await _forceCloseStockOrCrypto(ex, sym);
          unawaited(c._notifyOS('🛡️ Tight Stop-Loss: $sym', res.message));
          _stockLossBreach.remove(sym);
          _peakPlUsd.remove(sym);
          _peakGainBps.remove(sym);
          _entrySeenMs.remove(sym);
          peaksDirty = true;
        }
      } else {
        _stockLossBreach.remove(sym);
      }
    }

    _peakPlUsd.removeWhere((k, _) => !activeSyms.contains(k));
    _peakGainBps.removeWhere((k, _) => !activeSyms.contains(k));
    _entrySeenMs.removeWhere((k, _) => !activeSyms.contains(k));
    if (peaksDirty) _save();
  }

  Future<void> _protect""")

cg, n_somes = re.subn(
    r"Future<void> _runSomesAutoCashout\(\) async \{.*?\n\s*\}\s*\n\s*Future<void> _protect",
    new_somes_fn,
    cg,
    count=1,
    flags=re.DOTALL
)
print(f"[OK] Patched lib/shield_crypto_guardian.dart (matches={n_somes})")

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cg)

# ==============================================================================
# 4. PATCH lib/shield_controller.dart
#    - Block Crypto in _runAutonomousAgent (Equities & Inverse Bear ETFs only!)
#    - Require active green candle ignition (last.c > last.o) so we never buy falling knives
# ==============================================================================
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = clean_nbsp(f.read())

ctrl = re.sub(r"currentVersion = '[^']+';", "currentVersion = '7.0.0';", ctrl)

ctrl = re.sub(
    r"final candidates = _scanCandidates\(nowUtc\);",
    "final candidates = _scanCandidates(nowUtc).where((s) => !isCryptoSymbol(s)).toList();",
    ctrl,
    count=1
)

ctrl = re.sub(
    r"final trendAligned = [^;]+;",
    "final greenIgnition = last.c > last.o && closes.length >= 3 && last.c >= closes[closes.length - 2];\n  final trendAligned = !crypto && greenIgnition && ema9 >= ema21 && price > vwap && rsi >= 50.0 && rsi <= 65.0;",
    ctrl,
    count=1
)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Patched lib/shield_controller.dart (v3 Catalyst + Green Ignition + Crypto Blocked in AUTO)")

# ==============================================================================
# 5. PATCH lib/main.dart -> SHIELD v7.0
# ==============================================================================
with open('lib/main.dart', 'r', encoding='utf-8') as f:
    m = clean_nbsp(f.read())
m = re.sub(r"SHIELD v\d+\.\d+", "SHIELD v7.0", m)
with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(m)
print("[OK] Bumped UI title to SHIELD v7.0")
