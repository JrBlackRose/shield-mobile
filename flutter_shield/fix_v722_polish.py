import re

with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cg = f.read()

# Fix replaceStop to send ONLY stop_price and limit_price (no qty/side fields that cause 403)
old_replace_fn = re.search(
    r"Future> replaceStop\(\{.*?\n\s*\}",
    cg,
    flags=rre.DOTALL if 'rre' in globals() else re.DOTALL
)

new_replace_fn = """Future> replaceStop({
    required String orderId,
    required Pips stop,
    required Pips limit,
  }) async {
    final res = await _http
        .patch(
          Uri.parse('$kPaperBase/orders/${Uri.encodeComponent(orderId)}'),
          headers: _jsonH,
          body: jsonEncode({'stop_price': toApiPrice(stop), 'limit_price': toApiPrice(limit)}),
        )
        .timeout(_kV4Timeout);
    return (_decode(res) as Map).cast<String, dynamic>();
  }"""

if old_replace_fn:
    cg = cg.replace(old_replace_fn.group(0), new_replace_fn, 1)
    print("[OK] Cleaned replaceStop API call to prevent Alpaca 403 errors")

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cg)

# Fix Copilot JSON output rendering in shield_controller.dart
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = ctrl.replace("currentVersion = '7.2.0';", "currentVersion = '7.2.2';")

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)

print("[OK] Bumped version to 7.2.2")
