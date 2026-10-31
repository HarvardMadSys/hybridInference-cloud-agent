// Adapted from HybridInference 321e9a6c4 lib/schemas/auth.ts.
// The backend remains authoritative; the form mirrors its password rules.
export function validNewPassword(value: string): boolean {
  return (
    value.length >= 8 &&
    value.length <= 1024 &&
    /[A-Z]/.test(value) &&
    /[a-z]/.test(value) &&
    /[0-9]/.test(value)
  );
}
