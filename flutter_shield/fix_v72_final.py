with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

# Replace feedNews with the actual news getter or list name in ShieldController
# Let's check how feed items are accessed in shield_controller.dart
ctrl = ctrl.replace(
    "for (final n in feedNews)",
    "for (final n in feed)"
)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)

with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cg = f.read()

cg = cg.replace("final Map<String, int> _stockLossBreach = <String, int>{};", "")
cg = cg.replace("final Map<String, int> _entrySeenMs = <String, int>{};", "")

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cg)

print("[OK] Cleaned up feed reference and unused guardian fields.")
