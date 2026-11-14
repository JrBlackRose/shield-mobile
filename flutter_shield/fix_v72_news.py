import re

path = 'lib/shield_controller.dart'
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

pattern = re.compile(
    r"final newsSyms = <String>\{\s*"
    r"for \(final n in feed\)\s*"
    r"if \(n\.sentiment >= 0\.78\) \.\.\.n\.symbols\.map\(normSym\),\s*"
    r"\};"
)

replacement = """final newsSyms = <String>{
        for (final s in newsSignals)
          if (s.sentiment >= AutoRules.newsBuySentiment &&
              nowUtc.difference(s.at) <= AutoRules.newsFreshness)
            normSym(s.ticker),
      };"""

new_src, n = pattern.subn(replacement, src)
if n != 1:
    raise SystemExit(f"Expected 1 match, found {n}. File not changed.")

with open(path, 'w', encoding='utf-8') as f:
    f.write(new_src)
print("[OK] newsSyms now reads from newsSignals.")
