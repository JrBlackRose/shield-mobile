import re

# 1. Patch lib/shield_core.dart: Use gemini-3.0-flash & gemini-3.1-pro-preview + dynamic /v1beta/models auto-discovery
with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = f.read()

core = re.sub(
    r"const String kGeminiModel\s*=\s*'[^']+';",
    "const String kGeminiModel = 'gemini-3.0-flash';",
    core
)
core = re.sub(
    r"const List<String> kGeminiFallbackModels\s*=\s*\[[^\]]+\];",
    "const List<String> kGeminiFallbackModels = ['gemini-3.0-flash', 'gemini-3.1-pro-preview', 'gemini-3.1-flash-lite-preview'];",
    core
)

# Add dynamic model auto-discovery helper to GeminiClient so a 404 model name can NEVER break the app again
auto_discover_fn = """  static String? cachedWorkingModel;

  static Future<List<String>> resolveModels(http.Client http, String key) async {
    final base = <String>[
      if (cachedWorkingModel != null) cachedWorkingModel!,
      ...kGeminiFallbackModels,
    ];
    return base.toSet().toList();
  }

  static Future<String?> discoverActiveModel(http.Client http, String key) async {
    try {
      final res = await http
          .get(
            Uri.parse('https://generativelanguage.googleapis.com/v1beta/models'),
            headers: {'x-goog-api-key': key, 'accept': 'application/json'},
          )
          .timeout(const Duration(seconds: 10));
      if (res.statusCode != 200) return null;
      final j = jsonDecode(res.body);
      final list = j is Map ? j['models'] : null;
      if (list is! List) return null;
      final candidates = <String>[];
      for (final m in list.whereType<Map>()) {
        final name = '${m['name'] ?? ''}'.replaceFirst('models/', '');
        final methods = m['supportedGenerationMethods'];
        final supportsGen = methods is List && methods.map((e) => '$e').contains('generateContent');
        if (!supportsGen || !name.startsWith('gemini-')) continue;
        if (name.contains('embedding') || name.contains('imagen') || name.contains('veo') || name.contains('tts') || name.contains('audio')) continue;
        candidates.add(name);
      }
      // Prefer flash/pro 3.x models first
      candidates.sort((a, b) {
        int score(String s) {
          var v = 0;
          if (s.contains('3.')) v += 100;
          if (s.contains('flash') && !s.contains('lite')) v += 50;
          if (s.contains('pro')) v += 30;
          return -v;
        }
        return score(a).compareTo(score(b));
      });
      if (candidates.isNotEmpty) {
        cachedWorkingModel = candidates.first;
        return candidates.first;
      }
    } catch (_) {}
    return null;
  }
"""

if "discoverActiveModel" not in core:
    core = core.replace(
        "  Future<String> chatCopilot(String prompt) => _callGemini(prompt);",
        auto_discover_fn + "\n  Future<String> chatCopilot(String prompt) => _callGemini(prompt);"
    )

# Upgrade _callGemini in lib/shield_core.dart to use discovered model on 404
core = re.sub(
    r"  Future<String> _callGemini\(String promptText\) async \{.*?\n  \}",
    """  Future<String> _callGemini(String promptText) async {
    final models = await resolveModels(_http, _key);
    http.Response? res;
    Object? lastErr;
    var triedDiscovery = false;

    for (var i = 0; i < models.length; i++) {
      final m = models[i];
      try {
        res = await _http
            .post(
              Uri.parse('https://generativelanguage.googleapis.com/v1beta/models/$m:generateContent'),
              headers: {'x-goog-api-key': _key, 'Content-Type': 'application/json'},
              body: jsonEncode({
                'contents': [
                  {
                    'parts': [
                      {'text': promptText}
                    ]
                  }
                ],
                'generationConfig': {'responseMimeType': 'application/json'},
              }),
            )
            .timeout(_kReadTimeout);
      } catch (e) {
        lastErr = e;
        if (i < models.length - 1) {
          await Future<void>.delayed(Duration(milliseconds: 600 * (i + 1)));
          continue;
        }
        rethrow;
      }
      if (res.statusCode == 200) {
        GeminiClient.cachedWorkingModel = m;
        break;
      }
      if (res.statusCode == 404 && !triedDiscovery) {
        triedDiscovery = true;
        final discovered = await discoverActiveModel(_http, _key);
        if (discovered != null && !models.contains(discovered)) {
          models.add(discovered);
        }
      }
      if ((res.statusCode == 503 || res.statusCode == 429 || res.statusCode == 500 || res.statusCode == 404) &&
          i < models.length - 1) {
        await Future<void>.delayed(Duration(milliseconds: 600 * (i + 1)));
        continue;
      }
      break;
    }
    if (res == null && lastErr != null) throw lastErr;
    if (res == null || res.statusCode != 200) {
      final code = res?.statusCode ?? 503;
      var msg = 'HTTP $code';
      try {
        if (res != null) msg = (jsonDecode(res.body)['error']['message'] ?? msg).toString();
      } catch (_) {}
      throw ApiException('Gemini', code, msg);
    }
    try {
      final outer = jsonDecode(res.body) as Map<String, dynamic>;
      var text = (outer['candidates'][0]['content']['parts'][0]['text'] as String).trim();
      if (text.startsWith('```')) text = text.replaceAll(RegExp(r'^```(?:json)?|```$'), '').trim();
      return text;
    } catch (_) {
      throw const PlanAbort('Gemini returned a malformed response.');
    }
  }""",
    core,
    flags=re.DOTALL
)

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart (gemini-3.0-flash, gemini-3.1-pro-preview + Auto-Discovery)")

# 2. Patch lib/shield_controller.dart: Remove duplicate _activeSlotCount & upgrade _NewsScorer with Auto-Discovery
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

# Remove the duplicate _activeSlotCount definition (keep only the queuedBuySymbols one)
ctrl = ctrl.replace("  int get _activeSlotCount => {...heldSymbols, ...queuedSymbols}.length;\n\n", "")
ctrl = ctrl.replace("  int get _activeSlotCount => {...heldSymbols, ...queuedSymbols}.length;", "")

# Ensure queuedBuySymbols and _activeSlotCount exist exactly once
if "int get _activeSlotCount" not in ctrl:
    ctrl = ctrl.replace(
        "  Set<String> get heldSymbols =>",
        "  int get _activeSlotCount => {...heldSymbols, ...queuedBuySymbols}.length;\n  Set<String> get heldSymbols =>"
    )

# Don't pop up red error toast on background startup news scan
ctrl = ctrl.replace("scanNews(force: true)", "scanNews()")

# Upgrade _NewsScorer.score to also use GeminiClient.resolveModels & discoverActiveModel
ctrl = re.sub(
    r"    // Multi-model failover:.*?if \(res == null \|\| res\.statusCode != 200\)",
    """    // Multi-model failover + live /v1beta/models auto-discovery on 404
    final models = await GeminiClient.resolveModels(_http, _key);
    http.Response? res;
    Object? lastErr;
    var triedDiscovery = false;

    for (var i = 0; i < models.length; i++) {
      final m = models[i];
      try {
        res = await _http
            .post(
              Uri.parse('[https://generativelanguage.googleapis.com/v1beta/models/$m:generateContent](https://generativelanguage.googleapis.com/v1beta/models/$m:generateContent)'),
              headers: {'x-goog-api-key': _key, 'Content-Type': 'application/json'},
              body: jsonEncode({
                'contents': [
                  {
                    'parts': [
                      {'text': prompt}
                    ]
                  }
                ],
                'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.2},
              }),
            )
            .timeout(_kV4Timeout);
      } catch (e) {
        lastErr = e;
        if (i < models.length - 1) {
          await Future<void>.delayed(Duration(milliseconds: 600 * (i + 1)));
          continue;
        }
        rethrow;
      }
      if (res.statusCode == 200) {
        GeminiClient.cachedWorkingModel = m;
        break;
      }
      if (res.statusCode == 404 && !triedDiscovery) {
        triedDiscovery = true;
        final discovered = await GeminiClient.discoverActiveModel(_http, _key);
        if (discovered != null && !models.contains(discovered)) {
          models.add(discovered);
        }
      }
      if ((res.statusCode == 503 || res.statusCode == 429 || res.statusCode == 500 || res.statusCode == 404) &&
          i < models.length - 1) {
        await Future<void>.delayed(Duration(milliseconds: 600 * (i + 1)));
        continue;
      }
      break;
    }
    if (res == null && lastErr != null) throw lastErr;
    if (res == null || res.statusCode != 200)""",
    ctrl,
    flags=re.DOTALL
)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Patched lib/shield_controller.dart (Removed duplicate _activeSlotCount & added Auto-Discovery to _NewsScorer)")
