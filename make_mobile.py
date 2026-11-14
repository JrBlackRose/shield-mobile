import os

os.makedirs("flutter_shield/lib", exist_ok=True)
os.makedirs(".github/workflows", exist_ok=True)

dart_code = '''import 'dart:convert';
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
        colorScheme: const ColorScheme.dark(primary: Color(0xFF10B981), secondary: Color(0xFF38BDF8)),
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
      isAutoMode = prefs.getBool('auto_mode') ?? true;
    });
    if (alpacaKey.isNotEmpty) _fetchAccountData();
  }

  Future<void> _saveKeys(String ak, String as) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('alpaca_key', ak.trim());
    await prefs.setString('alpaca_secret', as.trim());
    setState(() { alpacaKey = ak.trim(); alpacaSecret = as.trim(); });
    _fetchAccountData();
  }

  Map<String, String> _getAlpacaHeaders() => {
    'APCA-API-KEY-ID': alpacaKey, 'APCA-API-SECRET-KEY': alpacaSecret, 'Content-Type': 'application/json'
  };

  void _addLog(String msg) {
    setState(() {
      eventFeed.insert(0, '${DateTime.now().toLocal().toString().substring(11, 19)} $msg');
      if (eventFeed.length > 20) eventFeed.removeLast();
    });
  }

  Future<void> _fetchAccountData() async {
    if (alpacaKey.isEmpty) return;
    try {
      final acctRes = await http.get(Uri.parse('https://paper-api.alpaca.markets/v2/account'), headers: _getAlpacaHeaders());
      if (acctRes.statusCode == 200) {
        final data = jsonDecode(acctRes.body);
        final eq = double.tryParse(data['equity']?.toString() ?? '0') ?? 0.0;
        final lastEq = double.tryParse(data['last_equity']?.toString() ?? '0') ?? eq;
        setState(() {
          equity = eq;
          buyingPower = double.tryParse(data['buying_power']?.toString() ?? '0') ?? 0.0;
          dailyPnL = eq - lastEq;
        });
      }
      final posRes = await http.get(Uri.parse('https://paper-api.alpaca.markets/v2/positions'), headers: _getAlpacaHeaders());
      if (posRes.statusCode == 200) setState(() => positions = jsonDecode(posRes.body));
      final ordRes = await http.get(Uri.parse('https://paper-api.alpaca.markets/v2/orders?status=open'), headers: _getAlpacaHeaders());
      if (ordRes.statusCode == 200) setState(() => orders = jsonDecode(ordRes.body));
    } catch (e) { _addLog('Network Error: $e'); }
  }

  Future<void> _executeTrade(String ticker, {bool isManualApproval = false}) async {
    if (alpacaKey.isEmpty) { _showKeyModal(); return; }
    _addLog('Routing $ticker... calculating limits.');
    try {
      final qRes = await http.get(Uri.parse('https://data.alpaca.markets/v2/stocks/$ticker/quotes/latest?feed=iex'), headers: _getAlpacaHeaders());
      double ask = 150.0;
      if (qRes.statusCode == 200) {
        ask = double.tryParse(jsonDecode(qRes.body)['quote']['ap']?.toString() ?? '150.0') ?? 150.0;
        if (ask == 0) ask = 150.0;
      }
      
      double stop = ask * 0.90;
      double target = ask * 1.30;
      int shares = ((equity * 0.01) / (ask - stop)).floor();
      int notionalCap = ((equity * 0.15) / ask).floor();
      if (shares > notionalCap) shares = notionalCap;
      if (shares < 1) shares = 1;
      double limitCollar = (ask * 1.0015 * 100).round() / 100;

      if (!isAutoMode && !isManualApproval) {
        _showApprovalDialog(ticker, shares, ask, limitCollar, stop, target);
        return;
      }

      final payload = {
        'symbol': ticker, 'qty': shares, 'side': 'buy', 'type': 'limit', 'limit_price': limitCollar,
        'time_in_force': 'gtc', 'order_class': 'bracket',
        'take_profit': {'limit_price': (target * 100).round() / 100},
        'stop_loss': {'stop_price': (stop * 100).round() / 100}
      };

      final oRes = await http.post(Uri.parse('https://paper-api.alpaca.markets/v2/orders'), headers: _getAlpacaHeaders(), body: jsonEncode(payload));
      if (oRes.statusCode == 200 || oRes.statusCode == 201) _addLog('✅ Executed: $shares sh $ticker @ <= \$$limitCollar');
      else _addLog('❌ Rejected: ${oRes.body}');
      _fetchAccountData();
    } catch (e) { _addLog('Execution Error: $e'); }
  }

  Future<void> _killSwitch() async {
    _addLog('🚨 KILL SWITCH: Canceling orders & flattening...');
    try {
      await http.delete(Uri.parse('https://paper-api.alpaca.markets/v2/orders'), headers: _getAlpacaHeaders());
      await http.delete(Uri.parse('https://paper-api.alpaca.markets/v2/positions'), headers: _getAlpacaHeaders());
      setState(() => isAutoMode = false);
      final p = await SharedPreferences.getInstance();
      await p.setBool('auto_mode', false);
      _fetchAccountData();
    } catch (e) { _addLog('Kill Error: $e'); }
  }

  void _showApprovalDialog(String ticker, int shares, double ask, double collar, double stop, double target) {
    showDialog(context: context, builder: (ctx) => AlertDialog(
      backgroundColor: const Color(0xFF0F172A),
      title: Text('📱 Approve: $ticker', style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
      content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('Shares: $shares', style: const TextStyle(color: Color(0xFF10B981))),
        Text('Collar: <=\$$collar\nStop: \$$stop\nTarget: \$$target', style: const TextStyle(color: Colors.white70)),
      ]),
      actions: [
        TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Reject', style: TextStyle(color: Colors.redAccent))),
        ElevatedButton(onPressed: () { Navigator.pop(ctx); _executeTrade(ticker, isManualApproval: true); }, child: const Text('Approve Fill')),
      ],
    ));
  }

  void _showKeyModal() {
    final akC = TextEditingController(text: alpacaKey);
    final asC = TextEditingController(text: alpacaSecret);
    showModalBottomSheet(context: context, isScrollControlled: true, backgroundColor: const Color(0xFF0F172A), builder: (ctx) => Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.of(ctx).viewInsets.bottom, left: 20, right: 20, top: 20),
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        const Text('🔐 Connect Alpaca (Stored locally)', style: TextStyle(color: Color(0xFF10B981), fontWeight: FontWeight.bold)),
        const SizedBox(height: 16),
        TextField(controller: akC, decoration: const InputDecoration(labelText: 'API Key ID', border: OutlineInputBorder())),
        const SizedBox(height: 10),
        TextField(controller: asC, obscureText: true, decoration: const InputDecoration(labelText: 'Secret Key', border: OutlineInputBorder())),
        const SizedBox(height: 16),
        ElevatedButton(onPressed: () { _saveKeys(akC.text, asC.text); Navigator.pop(ctx); }, child: const Text('Save & Connect')),
        const SizedBox(height: 20),
      ]),
    ));
  }

  @override
  Widget build(BuildContext context) {
    if (isWatchMode) return Scaffold(
      backgroundColor: Colors.black,
      body: Center(child: Container(width: 320, height: 320, padding: const EdgeInsets.all(16), decoration: BoxDecoration(shape: BoxShape.circle, border: Border.all(color: const Color(0xFF1E293B), width: 3)), child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
        const Text('EQUITY', style: TextStyle(fontSize: 10, color: Colors.white54)),
        Text('\$${equity.toStringAsFixed(0)}', style: const TextStyle(fontSize: 24, fontWeight: FontWeight.bold)),
        const SizedBox(height: 10),
        ElevatedButton(style: ElevatedButton.styleFrom(backgroundColor: isAutoMode ? const Color(0xFF10B981) : const Color(0xFF0284C7)), onPressed: () => setState(() => isAutoMode = !isAutoMode), child: Text(isAutoMode ? '🤖 AUTO' : '📱 MANUAL', style: const TextStyle(color: Colors.white))),
        const SizedBox(height: 6),
        ElevatedButton(style: ElevatedButton.styleFrom(backgroundColor: Colors.redAccent), onPressed: _killSwitch, child: const Text('🚨 KILL', style: TextStyle(color: Colors.white))),
        TextButton(onPressed: () => setState(() => isWatchMode = false), child: const Text('Exit', style: TextStyle(fontSize: 10, color: Colors.white38))),
      ]))),
    );

    return Scaffold(
      appBar: AppBar(backgroundColor: const Color(0xFF0F172A), title: const Text('🛡️ SHIELD APP'), actions: [
        IconButton(icon: const Icon(Icons.key, color: Color(0xFF10B981)), onPressed: _showKeyModal),
        IconButton(icon: const Icon(Icons.watch, color: Color(0xFF38BDF8)), onPressed: () => setState(() => isWatchMode = true)),
      ]),
      body: RefreshIndicator(onRefresh: _fetchAccountData, child: SingleChildScrollView(padding: const EdgeInsets.all(16), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Expanded(child: ElevatedButton(style: ElevatedButton.styleFrom(backgroundColor: isAutoMode ? const Color(0xFF10B981) : const Color(0xFF0284C7), padding: const EdgeInsets.symmetric(vertical: 12)), onPressed: () { setState(() => isAutoMode = !isAutoMode); }, child: Text(isAutoMode ? '🤖 AUTO MODE' : '📱 MANUAL MODE', style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)))),
          const SizedBox(width: 8),
          IconButton.filled(style: IconButton.filled(backgroundColor: Colors.redAccent), onPressed: _killSwitch, icon: const Icon(Icons.dangerous, color: Colors.white)),
        ]),
        const SizedBox(height: 16),
        Row(children: [ Expanded(child: _card('EQUITY', '\$${equity.toStringAsFixed(2)}', Colors.white)), const SizedBox(width: 8), Expanded(child: _card('TODAY P&L', '\$${dailyPnL.toStringAsFixed(2)}', dailyPnL >= 0 ? const Color(0xFF10B981) : Colors.redAccent)) ]),
        const SizedBox(height: 16),
        Card(child: Padding(padding: const EdgeInsets.all(14), child: Column(children: [
          Row(children: [
            Expanded(child: TextField(controller: tickerController, decoration: const InputDecoration(labelText: 'Ticker (e.g. NVDA)', border: OutlineInputBorder()))),
            const SizedBox(width: 8),
            ElevatedButton(style: ElevatedButton.styleFrom(backgroundColor: const Color(0xFF10B981), padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 18)), onPressed: () => _executeTrade(tickerController.text.trim().toUpperCase()), child: const Text('Execute', style: TextStyle(color: Colors.white))),
          ])
        ]))),
        const SizedBox(height: 16),
        const Text('⚡ Execution Feed', style: TextStyle(fontWeight: FontWeight.bold, color: Colors.white70)),
        Container(height: 150, margin: const EdgeInsets.top(6), padding: const EdgeInsets.all(10), decoration: BoxDecoration(color: const Color(0xFF0F172A), borderRadius: BorderRadius.circular(12)), child: ListView.builder(itemCount: eventFeed.length, itemBuilder: (ctx, i) => Text(eventFeed[i], style: const TextStyle(fontSize: 11, fontFamily: 'monospace', color: Color(0xFF38BDF8))))),
      ]))),
    );
  }

  Widget _card(String t, String v, Color c) => Container(padding: const EdgeInsets.all(14), decoration: BoxDecoration(color: const Color(0xFF0F172A), borderRadius: BorderRadius.circular(16)), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [ Text(t, style: const TextStyle(fontSize: 10, color: Colors.white54)), Text(v, style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: c)) ]));
}
'''

pubspec_yaml = '''name: flutter_shield
description: Project Shield Mobile
publish_to: 'none'
version: 1.0.0+1
environment:
  sdk: '>=3.0.0 <4.0.0'
dependencies:
  flutter:
    sdk: flutter
  http: ^1.2.0
  shared_preferences: ^2.2.3
flutter:
  uses-material-design: true
'''

github_action_yaml = '''name: Build Project Shield Android APK
on: [push, workflow_dispatch]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with:
          distribution: 'zulu'
          java-version: '17'
      - uses: subosito/flutter-action@v2
        with:
          flutter-version: '3.24.0'
          channel: 'stable'
      - name: Build APK
        run: |
          cd flutter_shield
          flutter pub get
          flutter build apk --release
      - name: Upload APK
        uses: actions/upload-artifact@v4
        with:
          name: ProjectShield-APK
          path: flutter_shield/build/app/outputs/flutter-apk/app-release.apk
'''

with open("flutter_shield/lib/main.dart", "w") as f: f.write(dart_code)
with open("flutter_shield/pubspec.yaml", "w") as f: f.write(pubspec_yaml)
with open(".github/workflows/build_apk.yml", "w") as f: f.write(github_action_yaml)

print("[+] All Mobile APK source files safely written!")
