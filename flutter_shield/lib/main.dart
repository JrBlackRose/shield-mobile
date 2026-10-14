import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

void main() => runApp(const ShieldApp());

class ShieldApp extends StatelessWidget {
  const ShieldApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Project Shield',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark().copyWith(
        scaffoldBackgroundColor: const Color(0xFF020617),
        cardColor: const Color(0xFF0F172A),
        colorScheme: const ColorScheme.dark(
          primary: Color(0xFF10B981),
          secondary: Color(0xFF38BDF8),
        ),
      ),
      home: const MainDashboard(),
    );
  }
}

class MainDashboard extends StatefulWidget {
  const MainDashboard({super.key});
  @override
  State<MainDashboard> createState() => _MainDashboardState();
}

class _MainDashboardState extends State<MainDashboard> {
  String alpacaKey = '';
  String alpacaSecret = '';
  String geminiKey = '';
  bool isAutoMode = true;
  bool isWatchMode = false;

  double equity = 0.0;
  double buyingPower = 0.0;
  double dailyPnL = 0.0;
  List<dynamic> positions = [];
  List<dynamic> orders = [];
  List<String> eventFeed = [];

  final TextEditingController tickerController = TextEditingController(text: 'NVDA');

  @override
  void initState() {
    super.initState();
    _loadKeys();
  }

  Future<void> _loadKeys() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      alpacaKey = prefs.getString('alpaca_key') ?? '';
      alpacaSecret = prefs.getString('alpaca_secret') ?? '';
      geminiKey = prefs.getString('gemini_key') ?? '';
      isAutoMode = prefs.getBool('auto_mode') ?? true;
    });
    if (alpacaKey.isNotEmpty && alpacaSecret.isNotEmpty) {
      _fetchAccountData();
    }
  }

  Future<void> _saveKeys(String ak, String as, String gk) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('alpaca_key', ak.trim());
    await prefs.setString('alpaca_secret', as.trim());
    await prefs.setString('gemini_key', gk.trim());
    setState(() {
      alpacaKey = ak.trim();
      alpacaSecret = as.trim();
      geminiKey = gk.trim();
    });
    _fetchAccountData();
  }

  Map<String, String> _getAlpacaHeaders() {
    return {
      'APCA-API-KEY-ID': alpacaKey,
      'APCA-API-SECRET-KEY': alpacaSecret,
      'Content-Type': 'application/json',
    };
  }

  Future<void> _fetchAccountData() async {
    if (alpacaKey.isEmpty || alpacaSecret.isEmpty) return;
    try {
      final acctRes = await http.get(
        Uri.parse('https://paper-api.alpaca.markets/v2/account'),
        headers: _getAlpacaHeaders(),
      );
      if (acctRes.statusCode == 200) {
        final data = jsonDecode(acctRes.body);
        final eq = double.tryParse(data['equity']?.toString() ?? '0') ?? 0.0;
        final lastEq = double.tryParse(data['last_equity']?.toString() ?? '0') ?? eq;
        final bp = double.tryParse(data['buying_power']?.toString() ?? '0') ?? 0.0;
        setState(() {
          equity = eq;
          buyingPower = bp;
          dailyPnL = eq - lastEq;
        });
      }

      final posRes = await http.get(
        Uri.parse('https://paper-api.alpaca.markets/v2/positions'),
        headers: _getAlpacaHeaders(),
      );
      if (posRes.statusCode == 200) {
        setState(() => positions = jsonDecode(posRes.body));
      }

      final ordRes = await http.get(
        Uri.parse('https://paper-api.alpaca.markets/v2/orders?status=open'),
        headers: _getAlpacaHeaders(),
      );
      if (ordRes.statusCode == 200) {
        setState(() => orders = jsonDecode(ordRes.body));
      }
    } catch (e) {
      _addLog('Network Error: $e');
    }
  }

  void _addLog(String msg) {
    setState(() {
      eventFeed.insert(0, '${DateTime.now().toLocal().toString().substring(11, 19)} $msg');
      if (eventFeed.length > 20) eventFeed.removeLast();
    });
  }

  // Pure On-Device Risk Math & Collared Bracket Execution
  Future<void> _executeTrade(String ticker, {bool isManualApproval = false}) async {
    if (alpacaKey.isEmpty) {
      _showKeyModal();
      return;
    }
    _addLog('Fetching latest quote for $ticker...');
    try {
      final quoteRes = await http.get(
        Uri.parse('https://data.alpaca.markets/v2/stocks/$ticker/quotes/latest?feed=iex'),
        headers: _getAlpacaHeaders(),
      );
      double askPrice = 150.0;
      if (quoteRes.statusCode == 200) {
        final qData = jsonDecode(quoteRes.body)['quote'];
        askPrice = double.tryParse(qData['ap']?.toString() ?? '150.0') ?? 150.0;
      }

      // Project Shield Math Rules
      double riskBudget = equity * 0.01; // 1.0% account risk
      double notionalCap = equity * 0.15; // 15% maximum portfolio position
      double stopLoss = askPrice * 0.90; // -10% stop
      double takeProfit = askPrice * 1.30; // +30% target (3:1 reward-to-risk)
      double riskPerShare = askPrice - stopLoss;

      int sharesFromRisk = (riskBudget / riskPerShare).floor();
      int sharesFromNotional = (notionalCap / askPrice).floor();
      int finalShares = sharesFromRisk < sharesFromNotional ? sharesFromRisk : sharesFromNotional;
      if (finalShares < 1) finalShares = 1;

      // Limit Buy Collar (+15 bps above ask)
      double limitCollar = (askPrice * 1.0015 * 100).round() / 100;

      if (!isAutoMode && !isManualApproval) {
        // Stage for manual approval in-app
        _showApprovalDialog(ticker, finalShares, askPrice, limitCollar, stopLoss, takeProfit);
        return;
      }

      // Submit Collared Limit Bracket Order directly to Alpaca
      final orderPayload = {
        'symbol': ticker,
        'qty': finalShares,
        'side': 'buy',
        'type': 'limit',
        'limit_price': limitCollar,
        'time_in_force': 'gtc',
        'order_class': 'bracket',
        'take_profit': {'limit_price': (takeProfit * 100).round() / 100},
        'stop_loss': {'stop_price': (stopLoss * 100).round() / 100},
      };

      final orderRes = await http.post(
        Uri.parse('https://paper-api.alpaca.markets/v2/orders'),
        headers: _getAlpacaHeaders(),
        body: jsonEncode(orderPayload),
      );

      if (orderRes.statusCode == 200 || orderRes.statusCode == 201) {
        _addLog('✅ Order Placed: $finalShares sh of $ticker @ Limit \$$limitCollar');
      } else {
        _addLog('❌ Order Rejected: ${orderRes.body}');
      }
      _fetchAccountData();
    } catch (e) {
      _addLog('Execution Error: $e');
    }
  }

  Future<void> _emergencyKillSwitch() async {
    _addLog('🚨 KILL SWITCH: Canceling all orders and flattening positions...');
    try {
      await http.delete(Uri.parse('https://paper-api.alpaca.markets/v2/orders'), headers: _getAlpacaHeaders());
      await http.delete(Uri.parse('https://paper-api.alpaca.markets/v2/positions'), headers: _getAlpacaHeaders());
      setState(() => isAutoMode = false);
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool('auto_mode', false);
      _addLog('🚨 Account Flattened. System switched to MANUAL mode.');
      _fetchAccountData();
    } catch (e) {
      _addLog('Kill Switch Error: $e');
    }
  }

  void _showApprovalDialog(String ticker, int shares, double ask, double collar, double stop, double target) {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF0F172A),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20), side: const BorderSide(color: Color(0xFF38BDF8))),
        title: Text('📱 Approve Trade: $ticker', style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Shares: $shares', style: const TextStyle(color: Color(0xFF10B981), fontWeight: FontWeight.bold)),
            Text('Limit Collar: <=\$$collar', style: const TextStyle(color: Colors.white70)),
            Text('Stop Loss: \$$stop (-10%)', style: const TextStyle(color: Colors.redAccent)),
            Text('Target: \$$target (+30%)', style: const TextStyle(color: Colors.greenAccent)),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Reject', style: TextStyle(color: Colors.redAccent))),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: const Color(0xFF10B981)),
            onPressed: () {
              Navigator.pop(ctx);
              _executeTrade(ticker, isManualApproval: true);
            },
            child: const Text('Approve Fill', style: TextStyle(color: Colors.white)),
          ),
        ],
      ),
    );
  }

  void _showKeyModal() {
    final akCtrl = TextEditingController(text: alpacaKey);
    final asCtrl = TextEditingController(text: alpacaSecret);
    final gkCtrl = TextEditingController(text: geminiKey);
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: const Color(0xFF0F172A),
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(24))),
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(bottom: MediaQuery.of(ctx).viewInsets.bottom, left: 20, right: 20, top: 20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('🔐 Connect Your Alpaca & Gemini Keys', style: TextStyle(color: Color(0xFF10B981), fontSize: 16, fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            const Text('Keys are stored securely only on your phone.', style: TextStyle(color: Colors.white60, fontSize: 12)),
            const SizedBox(height: 12),
            TextField(controller: akCtrl, decoration: const InputDecoration(labelText: 'Alpaca API Key ID', border: OutlineInputBorder())),
            const SizedBox(height: 10),
            TextField(controller: asCtrl, obscureText: true, decoration: const InputDecoration(labelText: 'Alpaca Secret Key', border: OutlineInputBorder())),
            const SizedBox(height: 10),
            TextField(controller: gkCtrl, obscureText: true, decoration: const InputDecoration(labelText: 'Gemini API Key (Optional)', border: OutlineInputBorder())),
            const SizedBox(height: 16),
            ElevatedButton(
              style: ElevatedButton.styleFrom(backgroundColor: const Color(0xFF10B981), minimumSize: const Size.fromHeight(48)),
              onPressed: () {
                _saveKeys(akCtrl.text, asCtrl.text, gkCtrl.text);
                Navigator.pop(ctx);
              },
              child: const Text('Save & Connect', style: TextStyle(fontWeight: FontWeight.bold, color: Colors.white)),
            ),
            const SizedBox(height: 20),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (isWatchMode) {
      return _buildWatchHUD();
    }
    return Scaffold(
      appBar: AppBar(
        backgroundColor: const Color(0xFF0F172A),
        title: const Text('🛡️ SHIELD MOBILE', style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
        actions: [
          IconButton(icon: const Icon(Icons.key, color: Color(0xFF10B981)), onPressed: _showKeyModal),
          IconButton(icon: const Icon(Icons.watch, color: Color(0xFF38BDF8)), onPressed: () => setState(() => isWatchMode = true)),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _fetchAccountData,
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Mode & Emergency Kill Banner
              Row(
                children: [
                  Expanded(
                    child: ElevatedButton(
                      style: ElevatedButton.styleFrom(
                        backgroundColor: isAutoMode ? const Color(0xFF10B981) : const Color(0xFF0284C7),
                        padding: const EdgeInsets.symmetric(vertical: 12),
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                      ),
                      onPressed: () async {
                        setState(() => isAutoMode = !isAutoMode);
                        final prefs = await SharedPreferences.getInstance();
                        await prefs.setBool('auto_mode', isAutoMode);
                        _addLog('Switched Mode to ${isAutoMode ? "AUTO" : "MANUAL"}');
                      },
                      child: Text(isAutoMode ? '🤖 AUTO MODE ON' : '📱 MANUAL CARD MODE', style: const TextStyle(fontWeight: FontWeight.bold, color: Colors.white)),
                    ),
                  ),
                  const SizedBox(width: 8),
                  IconButton.filled(
                    style: IconButton.filled(backgroundColor: Colors.redAccent),
                    onPressed: _emergencyKillSwitch,
                    icon: const Icon(Icons.dangerous, color: Colors.white),
                  ),
                ],
              ),
              const SizedBox(height: 16),

              // Account Metric Cards
              Row(
                children: [
                  _metricCard('EQUITY', '\$${equity.toStringAsFixed(2)}', Colors.white),
                  const SizedBox(width: 8),
                  _metricCard('TODAY P&L', '${dailyPnL >= 0 ? "+" : ""}\$${dailyPnL.toStringAsFixed(2)}', dailyPnL >= 0 ? const Color(0xFF10B981) : Colors.redAccent),
                ],
              ),
              const SizedBox(height: 16),

              // 1-Click Order Execution
              Card(
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
                child: Padding(
                  padding: const EdgeInsets.all(14),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('🚀 Launch Collared Limit Order', style: TextStyle(fontWeight: FontWeight.bold)),
                      const SizedBox(height: 10),
                      Row(
                        children: [
                          Expanded(child: TextField(controller: tickerController, decoration: const InputDecoration(labelText: 'Ticker (e.g. NVDA, ALB)', border: OutlineInputBorder()))),
                          const SizedBox(width: 8),
                          ElevatedButton(
                            style: ElevatedButton.styleFrom(backgroundColor: const Color(0xFF10B981), padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 18)),
                            onPressed: () => _executeTrade(tickerController.text.trim().toUpperCase()),
                            child: const Text('Execute', style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
                          )
                        ],
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 16),

              // Live Event Feed
              const Text('⚡ Instant Execution & Safety Feed', style: TextStyle(fontSize: 12, fontWeight: FontWeight.bold, color: Colors.white70)),
              const SizedBox(height: 6),
              Container(
                height: 150,
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(color: const Color(0xFF0F172A), borderRadius: BorderRadius.circular(12), border: Border.all(color: Colors.white10)),
                child: eventFeed.isEmpty
                    ? const Center(child: Text('No trades yet. System armed.', style: TextStyle(color: Colors.white30, fontSize: 12)))
                    : ListView.builder(
                        itemCount: eventFeed.length,
                        itemBuilder: (ctx, i) => Text(eventFeed[i], style: const TextStyle(fontSize: 11, fontFamily: 'monospace', color: Color(0xFF38BDF8))),
                      ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  // 4. Smartwatch Circular Wrist HUD
  Widget _buildWatchHUD() {
    return Scaffold(
      backgroundColor: Colors.black,
      body: Center(
        child: Container(
          width: 320,
          height: 320,
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(shape: BoxShape.circle, border: Border.all(color: const Color(0xFF1E293B), width: 3)),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const Text('MY EQUITY', style: TextStyle(fontSize: 10, color: Colors.white54)),
              Text('\$${equity.toStringAsFixed(0)}', style: const TextStyle(fontSize: 22, fontWeight: FontWeight.extrabold, color: Colors.white)),
              Text('${dailyPnL >= 0 ? "+" : ""}\$${dailyPnL.toStringAsFixed(1)}', style: TextStyle(fontSize: 12, fontWeight: FontWeight.bold, color: dailyPnL >= 0 ? const Color(0xFF10B981) : Colors.redAccent)),
              const SizedBox(height: 10),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: isAutoMode ? const Color(0xFF10B981) : const Color(0xFF0284C7),
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
                ),
                onPressed: () => setState(() => isAutoMode = !isAutoMode),
                child: Text(isAutoMode ? '🤖 AUTO' : '📱 MANUAL', style: const TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: Colors.white)),
              ),
              const SizedBox(height: 6),
              ElevatedButton(
                style: ElevatedButton.styleFrom(backgroundColor: Colors.redAccent, padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6)),
                onPressed: _emergencyKillSwitch,
                child: const Text('🚨 KILL ALL', style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: Colors.white)),
              ),
              TextButton(onPressed: () => setState(() => isWatchMode = false), child: const Text('Exit Watch', style: TextStyle(fontSize: 10, color: Colors.white38))),
            ],
          ),
        ),
      ),
    );
  }

  Widget _metricCard(String title, String val, Color valColor) {
    return Expanded(
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(color: const Color(0xFF0F172A), borderRadius: BorderRadius.circular(16), border: Border.all(color: Colors.white10)),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: const TextStyle(fontSize: 10, color: Colors.white54, fontWeight: FontWeight.bold)),
            const SizedBox(height: 4),
            Text(val, style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: valColor)),
          ],
        ),
      ),
    );
  }
}
