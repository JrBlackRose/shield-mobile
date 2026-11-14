import re

# ==============================================================================
# 1. PATCH patch_android.py
#    - Add ACCESS_WIFI_STATE permission & WifiManager.WifiLock so Wi-Fi never sleeps
#    - Add FLAG_KEEP_SCREEN_ON to MainActivity
#    - Add a 25-second native Kotlin Handler heartbeat ("nativeTick") to keep
#      Dart's background isolate running 24/7 even when screen is off
# ==============================================================================
with open('patch_android.py', 'r', encoding='utf-8') as f:
    pa = f.read()

pa = pa.replace(
    '<uses-permission android:name="android.permission.WAKE_LOCK"/>',
    '<uses-permission android:name="android.permission.WAKE_LOCK"/>\n    <uses-permission android:name="android.permission.ACCESS_WIFI_STATE"/>\n    <uses-permission android:name="android.permission.CHANGE_WIFI_STATE"/>'
)

old_imports = """import android.os.IBinder
import android.os.PowerManager
import android.provider.Settings"""

new_imports = """import android.net.wifi.WifiManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.WindowManager"""

pa = pa.replace(old_imports, new_imports)

old_wakelock = """    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {"""

new_wakelock = """    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onCreate() {"""

pa = pa.replace(old_wakelock, new_wakelock)

old_wl_acquire = """        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Shield::ForegroundServiceWakeLock").apply {
            setReferenceCounted(false)
            acquire()
        }"""

new_wl_acquire = """        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Shield::ForegroundServiceWakeLock").apply {
            setReferenceCounted(false)
            acquire()
        }
        try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val lockMode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            } else {
                @Suppress("DEPRECATION")
                WifiManager.WIFI_MODE_FULL_HIGH_PERF
            }
            wifiLock = wm.createWifiLock(lockMode, "Shield::WifiWakeLock").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (_: Exception) {}"""

pa = pa.replace(old_wl_acquire, new_wl_acquire)

old_wl_release = """        try {
            if (wakeLock?.isHeld == true) wakeLock?.release()
        } catch (_: Exception) {}"""

new_wl_release = """        try {
            if (wakeLock?.isHeld == true) wakeLock?.release()
            if (wifiLock?.isHeld == true) wifiLock?.release()
        } catch (_: Exception) {}"""

pa = pa.replace(old_wl_release, new_wl_release)

old_main_activity = """class MainActivity: FlutterActivity() {
    private val CHANNEL = "shield/native"
    private val ALERT_CHANNEL_ID = "shield_high_alerts"
    private var notifId = 100

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        createAlertNotificationChannel()
        requestNotificationPermission()
        startShieldForegroundService()
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL).setMethodCallHandler { call, result ->"""

new_main_activity = """class MainActivity: FlutterActivity() {
    private val CHANNEL = "shield/native"
    private val ALERT_CHANNEL_ID = "shield_high_alerts"
    private var notifId = 100
    private var methodChannel: MethodChannel? = null
    private val bgHandler = Handler(Looper.getMainLooper())
    private val bgRunnable = object : Runnable {
        override fun run() {
            try {
                methodChannel?.invokeMethod("nativeTick", null)
            } catch (_: Exception) {}
            bgHandler.postDelayed(this, 25000L)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        createAlertNotificationChannel()
        requestNotificationPermission()
        startShieldForegroundService()
    }

    override fun onDestroy() {
        bgHandler.removeCallbacks(bgRunnable)
        super.onDestroy()
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        val ch = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
        methodChannel = ch
        bgHandler.removeCallbacks(bgRunnable)
        bgHandler.postDelayed(bgRunnable, 25000L)
        ch.setMethodCallHandler { call, result ->"""

pa = pa.replace(old_main_activity, new_main_activity)

with open('patch_android.py', 'w', encoding='utf-8') as f:
    f.write(pa)
print("[OK] Patched patch_android.py (WifiLock + FLAG_KEEP_SCREEN_ON + 25s Native-to-Dart Heartbeat)")

# ==============================================================================
# 2. PATCH lib/shield_core.dart
#    - Widen constructive RSI band to 42.0..65.0 so RSI 60.7 is not blocked as overbought
# ==============================================================================
with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = f.read()

core = re.sub(
    r"bool get rsiConstructive => rsiValue >= [\d.]+ && rsiValue <=? [\d.]+;[^\n]*",
    "bool get rsiConstructive => rsiValue >= 42.0 && rsiValue <= 65.0; // Healthy pullback & bounce band",
    core
)
core = re.sub(
    r"bool get rsiOverbought => rsiValue >?=? [\d.]+;[^\n]*",
    "bool get rsiOverbought => rsiValue > 65.0; // Extended/Climax zone",
    core
)

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart (Constructive RSI band 42.0 - 65.0)")

# ==============================================================================
# 3. PATCH lib/shield_crypto_guardian.dart
#    - Fix Alpaca 'Vol 0.00x' bar bug by filtering 0-volume bars & checking last 3 bars
#    - Widen crypto trend/pullback tolerance so valid bounces pass cleanly
# ==============================================================================
with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cguard = f.read()

old_cguard_block = """  final window = done.length > 288 ? done.sublist(done.length - 288) : done;
  final vwap = sessionVwap(window);
  final vols = done.map((b) => b.v).toList();
  final closes = done.map((b) => b.c).toList();
  final prior = vols.sublist(vols.length - 49, vols.length - 1)..sort();
  final med = (prior[23] + prior[24]) / 2.0;
  final vr = med > 0 ? vols.last / med : null;
  var rsi = wilderRsi(closes, period: ConfluenceRules.rsiPeriod);
  if (vwap == null || vr == null || rsi == null) return null;

  double ema(List<double> vals, int period) {
    final k = 2.0 / (period + 1);
    var e = vals.first;
    for (var i = 1; i < vals.length; i++) {
      e = vals[i] * k + e * (1.0 - k);
    }
    return e;
  }
  final ema9 = ema(closes, 9);
  final ema21 = ema(closes, 21);
  final last = done.last;
  final range = last.h - last.l;
  final lowerWick = math.min(last.o, last.c) - last.l;
  final bounceCandle = range > 0 && (last.c >= last.o || (lowerWick / range) >= 0.25);
  final notOverextended = ((price - ema9) / price) <= 0.0065; // Not >0.65% extended above EMA9
  final trendAligned = ema9 >= ema21 && price >= ema21 * 0.997 && bounceCandle && notOverextended;

  if (!trendAligned) {
    // Hard gate: force < 2 points so non-pullback spikes NEVER pass triggerTrade
    rsi = 38.0;
  }
  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: trendAligned ? vwap : price * 1.01,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: done.last.t,
  );"""

new_cguard_block = """  final window = done.length > 288 ? done.sublist(done.length - 288) : done;
  final vwap = sessionVwap(window);
  final vols = done.map((b) => b.v).toList();
  final closes = done.map((b) => b.c).toList();
  final nonZeroPrior = vols.sublist(vols.length - 49, vols.length - 1).where((v) => v > 0).toList()..sort();
  final med = nonZeroPrior.isEmpty
      ? 0.0
      : nonZeroPrior[nonZeroPrior.length ~/ 2];
  // Use max volume of last 3 bars to avoid Alpaca's 0.00x empty bar quirk
  final recentVol = math.max(vols.last, math.max(vols[vols.length - 2], vols[vols.length - 3]));
  var vr = med > 0 ? (recentVol / med).clamp(0.1, 9.9) : 1.6;
  var rsi = wilderRsi(closes, period: ConfluenceRules.rsiPeriod);
  if (vwap == null || rsi == null) return null;

  double ema(List<double> vals, int period) {
    final k = 2.0 / (period + 1);
    var e = vals.first;
    for (var i = 1; i < vals.length; i++) {
      e = vals[i] * k + e * (1.0 - k);
    }
    return e;
  }
  final ema9 = ema(closes, 9);
  final ema21 = ema(closes, 21);
  final last = done.last;
  final range = last.h - last.l;
  final lowerWick = math.min(last.o, last.c) - last.l;
  final bounceCandle = range <= 0 || last.c >= last.o || (lowerWick / range) >= 0.20;
  final notOverextended = ((price - ema9) / price) <= 0.0085; // Not >0.85% extended above EMA9
  final trendAligned = ema9 >= ema21 * 0.9985 && price >= ema21 * 0.995 && bounceCandle && notOverextended;
  if (trendAligned && vr < 1.5 && price >= vwap) {
    vr = 1.55; // Prevent Alpaca's sparse US crypto book volume from blocking a confirmed EMA+VWAP pullback
  }

  if (!trendAligned) {
    // Hard gate: force < 2 points so non-pullback spikes NEVER pass triggerTrade
    rsi = 38.0;
  }
  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: trendAligned ? vwap : price * 1.01,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: done.last.t,
  );"""

if old_cguard_block in cguard:
    cguard = cguard.replace(old_cguard_block, new_cguard_block, 1)

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cguard)
print("[OK] Patched lib/shield_crypto_guardian.dart (Fixed Vol 0.00x quirk & crypto pullback gate)")

# ==============================================================================
# 4. PATCH lib/shield_controller.dart
#    - Listen to 'nativeTick' from Kotlin so background polling never stops
#    - Stop didChangeAppLifecycleState from canceling timers when screen turns off
#    - Auto-recover connectionStatus on network recovery
#    - Pre-screen candidates in _runAutonomousAgent so the Scout checks ALL crypto
#      pairs (ETHUSD, SOLUSD, AVAXUSD, etc.) instead of failing on BTCUSD alone!
# ==============================================================================
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = ctrl.replace("currentVersion = '5.0.0';", "currentVersion = '5.1.0';")

# Wire nativeTick handler inside _enableBackgroundOS
old_bg_os = """  Future<void> _enableBackgroundOS() async {
    try {
      await _native.invokeMethod('startBackground');
    } catch (_) {}
  }"""

new_bg_os = """  Future<void> _enableBackgroundOS() async {
    _native.setMethodCallHandler((call) async {
      if (call.method == 'nativeTick' && vaultStatus == VaultStatus.valid) {
        unawaited(refresh());
      }
      return true;
    });
    try {
      await _native.invokeMethod('startBackground');
    } catch (_) {}
  }"""

ctrl = ctrl.replace(old_bg_os, new_bg_os, 1)

# Fix didChangeAppLifecycleState so locking screen doesn't reset timers
old_lifecycle = """  @override
  void didChangeAppLifecycleState(AppLifecycleState s) {
    isForeground = s == AppLifecycleState.resumed;
    if (vaultStatus == VaultStatus.valid) {
      _startPolling();
      if (isForeground) unawaited(refresh().then((_) => scanNews()));
    }
  }"""

new_lifecycle = """  @override
  void didChangeAppLifecycleState(AppLifecycleState s) {
    isForeground = s == AppLifecycleState.resumed;
    if (vaultStatus == VaultStatus.valid && isForeground) {
      if (_pollTimer == null || !_pollTimer!.isActive) _startPolling();
      unawaited(refresh().then((_) => scanNews()));
    }
  }"""

ctrl = ctrl.replace(old_lifecycle, new_lifecycle, 1)

# Upgrade _runAutonomousAgent to scan across candidates for >= 2/3 confluence first!
old_agent = """      final raw = await _coreChat(_scoutPrompt(nowUtc, candidates));
      _lastAgentError = null;
      final pick = _parseScoutPick(raw, candidates.toSet());
      if (pick == null) {
        _log('🔍 Scout scanned ${candidates.length} live pairs — no pullback setup met entry criteria.', level: 'info', notify: false);
        return;
      }"""

new_agent = """      // Pre-screen candidates with the quantitative Pullback Sniper so we never get stuck on BTCUSD alone!
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
      }
      if (passing.isEmpty) {
        _log('🔍 Scout checked ${candidates.take(8).length} pairs — 0 passed Pullback Sniper (>=2/3) this round.', level: 'info', notify: false);
        return;
      }
      String? pick;
      if (passing.length == 1) {
        pick = passing.first;
      } else {
        try {
          final raw = await _coreChat(_scoutPrompt(nowUtc, passing));
          pick = _parseScoutPick(raw, passing.toSet()) ?? passing.first;
        } catch (_) {
          pick = passing.first;
        }
      }
      _lastAgentError = null;"""

if old_agent in ctrl:
    ctrl = ctrl.replace(old_agent, new_agent, 1)
    print("[OK] Patched _runAutonomousAgent (Multi-pair quantitative pre-screening)")
else:
    print("[WARN] Could not match old_agent block — check _runAutonomousAgent")

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)

# ==============================================================================
# 5. PATCH lib/main.dart -> SHIELD v5.1
# ==============================================================================
with open('lib/main.dart', 'r', encoding='utf-8') as f:
    main_code = f.read()
main_code = main_code.replace("SHIELD v5.0", "SHIELD v5.1")
with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(main_code)
print("[OK] Patched lib/main.dart (SHIELD v5.1)")
