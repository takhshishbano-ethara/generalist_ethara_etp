(() => {
  const result = {
    has_auth: false,
    login_forms: [],
    oauth_providers: [],
    protected_indicators: [],
    cookie_consent: null,
    cookies: [],
    auth_meta: {},
  };

  // --- LOGIN FORMS ---
  const passwordInputs = document.querySelectorAll('input[type="password"]');
  if (passwordInputs.length > 0) {
    result.has_auth = true;
    passwordInputs.forEach(input => {
      const form = input.closest('form');
      if (form) {
        result.login_forms.push({
          action: form.action || null,
          method: form.method || 'get',
          fields: Array.from(form.querySelectorAll('input')).map(i => ({
            type: i.type,
            name: i.name || i.id,
            placeholder: i.placeholder || null,
          })),
        });
      }
    });
  }

  // Check for login-related forms even without password fields
  const loginForms = document.querySelectorAll('form[action*="login"], form[action*="signin"], form[action*="auth"], form[action*="register"], form[action*="signup"]');
  loginForms.forEach(form => {
    result.has_auth = true;
    if (!result.login_forms.some(f => f.action === form.action)) {
      result.login_forms.push({
        action: form.action || null,
        method: form.method || 'get',
        fields: Array.from(form.querySelectorAll('input')).map(i => ({
          type: i.type,
          name: i.name || i.id,
        })),
      });
    }
  });

  // --- OAUTH PROVIDERS ---
  const oauthPatterns = [
    { provider: 'Google', selectors: ['[data-provider="google"]', '[class*="google"]', 'a[href*="accounts.google.com"]', 'button[aria-label*="Google"]'] },
    { provider: 'GitHub', selectors: ['[data-provider="github"]', 'a[href*="github.com/login"]', 'button[aria-label*="GitHub"]'] },
    { provider: 'Facebook', selectors: ['[data-provider="facebook"]', 'a[href*="facebook.com/login"]', '.fb-login-button'] },
    { provider: 'Apple', selectors: ['[data-provider="apple"]', 'a[href*="appleid.apple.com"]'] },
    { provider: 'Twitter/X', selectors: ['[data-provider="twitter"]', 'a[href*="twitter.com/oauth"]'] },
    { provider: 'Discord', selectors: ['[data-provider="discord"]', 'a[href*="discord.com/oauth"]', 'a[href*="discord.com/api/oauth"]'] },
    { provider: 'Microsoft', selectors: ['[data-provider="microsoft"]', 'a[href*="login.microsoftonline.com"]'] },
  ];

  for (const { provider, selectors } of oauthPatterns) {
    for (const sel of selectors) {
      try {
        if (document.querySelector(sel)) {
          result.has_auth = true;
          result.oauth_providers.push(provider);
          break;
        }
      } catch (e) {}
    }
  }

  // Generic OAuth button detection via text content
  const buttons = document.querySelectorAll('button, a[role="button"], [class*="btn"], [class*="button"]');
  const oauthTexts = /sign\s*in\s*with|log\s*in\s*with|continue\s*with|connect\s*with/i;
  buttons.forEach(btn => {
    const text = btn.textContent?.trim();
    if (text && oauthTexts.test(text)) {
      result.has_auth = true;
      const match = text.match(/(?:sign\s*in|log\s*in|continue|connect)\s*with\s+(\w+)/i);
      if (match && !result.oauth_providers.includes(match[1])) {
        result.oauth_providers.push(match[1]);
      }
    }
  });

  // --- PROTECTED ROUTE INDICATORS ---
  // Meta refresh redirects
  const metaRefresh = document.querySelector('meta[http-equiv="refresh"]');
  if (metaRefresh) {
    const content = metaRefresh.content || '';
    if (content.toLowerCase().includes('login') || content.toLowerCase().includes('auth')) {
      result.protected_indicators.push({ type: 'meta_refresh', target: content });
    }
  }

  // Auth-related meta tags
  const csrfMeta = document.querySelector('meta[name="csrf-token"], meta[name="_csrf"]');
  if (csrfMeta) result.auth_meta.csrf = true;

  // Check for auth SDK globals
  const authChecks = {
    'Clerk': () => window.Clerk !== undefined,
    'Auth0': () => window.auth0 !== undefined,
    'Firebase Auth': () => window.firebase?.auth !== undefined,
    'Supabase Auth': () => window.supabase?.auth !== undefined,
    'NextAuth': () => document.querySelector('script[src*="next-auth"]') !== null,
    'Passport.js': () => document.cookie.includes('connect.sid'),
  };

  for (const [name, check] of Object.entries(authChecks)) {
    try {
      if (check()) {
        result.has_auth = true;
        result.auth_meta.sdk = name;
        break;
      }
    } catch (e) {}
  }

  // --- COOKIE CONSENT BANNERS ---
  const consentSelectors = [
    '[class*="cookie-consent"]', '[class*="cookieConsent"]', '[class*="cookie-banner"]',
    '[class*="CookieBanner"]', '[id*="cookie"]', '[class*="gdpr"]', '[id*="gdpr"]',
    '[class*="consent"]', '[id*="consent"]', '[class*="privacy-banner"]',
    '[data-testid*="cookie"]', '[aria-label*="cookie"]',
  ];

  for (const sel of consentSelectors) {
    try {
      const el = document.querySelector(sel);
      if (el && el.offsetHeight > 0) {
        result.cookie_consent = {
          detected: true,
          selector: sel,
          text: el.textContent?.substring(0, 200)?.trim(),
        };
        break;
      }
    } catch (e) {}
  }

  // --- COOKIES ---
  try {
    const cookies = document.cookie.split(';').map(c => c.trim()).filter(Boolean);
    result.cookies = cookies.map(c => {
      const [name] = c.split('=');
      const isAuth = /sess|token|auth|jwt|sid|user|login|csrf/i.test(name);
      return { name: name.trim(), isAuthRelated: isAuth };
    });
  } catch (e) {}

  // --- NAVIGATION LINKS suggesting auth ---
  const navLinks = document.querySelectorAll('nav a, header a, [class*="nav"] a');
  navLinks.forEach(link => {
    const text = link.textContent?.trim().toLowerCase();
    const href = link.href || '';
    if (text && /^(log\s*in|sign\s*in|sign\s*up|register|my\s*account|dashboard|profile)$/.test(text)) {
      result.has_auth = true;
      result.protected_indicators.push({ type: 'nav_link', text, href });
    }
  });

  return result;
})();
