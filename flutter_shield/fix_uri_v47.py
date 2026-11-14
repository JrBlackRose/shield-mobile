import glob
import re

# 1. Strip any accidental Markdown link brackets [url](url) across all lib/*.dart files
for path in glob.glob('lib/*.dart'):
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    cleaned = re.sub(r'\[(https?://[^\]\s]+)\]\(\1\)', r'\1', text)
    cleaned = re.sub(r'\[(https?://[^\]\s]+)\]\([^)]+\)', r'\1', cleaned)
    if cleaned != text:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(cleaned)
        print(f"[CLEANED] Removed clipboard markdown brackets in {path}")

# 2. Patch lib/shield_core.dart to use Uri.https (immune to clipboard link mangling)
with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = f.read()

core = re.sub(
    r"const String kGeminiModel\s*=\s*'[^']+';",
    "const String kGeminiModel = 'gemini-2.5-flash';",
    core
)
core = re.sub(
    r"const List<String> kGeminiFallbackModels\s*=\s*\[[^\]]+\];",
    "const List<String> kGeminiFallbackModels = ['gemini-2.5-flash', 'gemini-3-flash-preview', 'gemini-2.5-flash-lite', 'gemini-3.1-pro-preview'];",
    core
)

new_gemini_client_methods = """  static String? cachedWorkingModel;
  static List<String>? _discoveredModels;
  static final Set<String> deadModels = <String>{};

  static Future<List<String>> resolveModels(http.Client http, String key) async {
    if (_discoveredModels == null) {
      await discoverActiveModel(http, key);
    }
    final out = <String>[
      if (cachedWorkingModel != null && !deadModels.contains(cachedWorkingModel)) cachedWorkingModel!,
      ...?_discoveredModels,
      ...kGeminiFallbackModels,
    ].where((m) => m.isNotEmpty && !deadModels.contains(m)).toSet().toList();
    return out.isEmpty ? kGeminiFallbackModels : out;
  }

  static Future<String?> discoverActiveModel(http.Client http, String key) async {
    try {
      final uri = Uri.https('generativelanguage.googleapis.com', '/v1beta/models');
      final res = await http
          .get(uri, headers: {'x-goog-api-key': key, 'accept': 'application/json'})
          .timeout(const Duration(seconds: 10));
      if (res.statusCode != 200) return null;
      final j = jsonDecode(res.body);
      final list = j is Map ? j['models'] : null;
      if (list is! List) return null;
      final skip = ['image', 'imagen', 'veo', 'tts', 'audio', 'embedding', 'aqa', 'robotics', 'computer', '2.0'];
      final candidates = <String>[];
      for (final m in list.whereType<Map>()) {
        final name = '${m['name'] ?? ''}'.replaceFirst('models/', '').trim();
        final methods = m['supportedGenerationMethods'];
        final supportsGen = methods is List && methods.map((e) => '$e').contains('generateContent');
        if (!supportsGen || !name.startsWith('gemini-') || deadModels.contains(name)) continue;
        if (skip.any((s) => name.contains(s))) continue;
        candidates.add(name);
      }
      candidates.sort((a, b) {
        int rank(String s) {
          var v = 0;
          if (s.contains('flash') && !s.contains('lite')) v += 100;
          if (s.contains('flash-lite')) v += 80;
          if (s.contains('3')) v += 25;
          if (s.contains('2.5')) v += 20;
          if (s.contains('pro')) v += 10;
          return -v;
        }
        return rank(a).compareTo(rank(b));
      });
      if (candidates.isNotEmpty) {
        _discoveredModels = candidates;
        cachedWorkingModel ??= candidates.first;
        return candidates.first;
      }
    } catch (_) {}
    return null;
  }

  Future<String> chatCopilot(String prompt) => _callGemini(prompt);

  Future<String> _callGemini(String promptText) async {
    final models = await resolveModels(_http, _key);
    http.Response? res;
    Object? lastErr;

    for (var i = 0; i < models.length; i++) {
      final m = models[i].trim();
      final uri = Uri.https('generativelanguage.googleapis.com', '/v1beta/models/$m:generateContent');
      try {
        res = await _http
            .post(
              uri,
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
          await Future<void>.delayed(Duration(milliseconds: 500 * (i + 1)));
          continue;
        }
        rethrow;
      }
      if (res.statusCode == 200) {
        cachedWorkingModel = m;
        break;
      }
      if (res.statusCode == 404) {
        deadModels.add(m);
      }
      if ((res.statusCode == 503 || res.statusCode == 429 || res.statusCode == 500 || res.statusCode == 404) &&
          i < models.length - 1) {
        await Future<void>.delayed(Duration(milliseconds: 500 * (i + 1)));
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
  }"""

core = re.sub(
    r"  static String\? cachedWorkingModel;.*?  Future<String> _callGemini\(String promptText\) async \{.*?\n  \}",
    new_gemini_client_methods,
    core,
    flags=re.DOTALL
)

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart with Uri.https()")

# 3. Patch lib/shield_controller.dart _NewsScorer to use Uri.https()
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = re.sub(
    r"    // Multi-model failover.*?    if \(res == null \|\| res\.statusCode != 200\)",
    """    // Multi-model failover using Uri.https() + automatic dead-model pruning
    final models = await GeminiClient.resolveModels(_http, _key);
    http.Response? res;
    Object? lastErr;

    for (var i = 0; i < models.length; i++) {
      final m = models[i].trim();
      final uri = Uri.https('generativelanguage.googleapis.com', '/v1beta/models/$m:generateContent');
      try {
        res = await _http
            .post(
              uri,
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
          await Future<void>.delayed(Duration(milliseconds: 500 * (i + 1)));
          continue;
        }
        rethrow;
      }
      if (res.statusCode == 200) {
        GeminiClient.cachedWorkingModel = m;
        break;
      }
      if (res.statusCode == 404) {
        GeminiClient.deadModels.add(m);
      }
      if ((res.statusCode == 503 || res.statusCode == 429 || res.statusCode == 500 || res.statusCode == 404) &&
          i < models.length - 1) {
        await Future<void>.delayed(Duration(milliseconds: 500 * (i + 1)));
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
print("[OK] Patched lib/shield_controller.dart with Uri.https()")
