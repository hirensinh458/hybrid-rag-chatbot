// rag-super-admin/src/supabase.js
//
// ── FIX: Added auth persistence options ──────────────────────────────────────
//
// BEFORE (broken):
//   export const supabase = createClient(supabaseUrl, supabaseAnon)
//
//   Without explicit auth options, the Supabase client does not reliably
//   persist the session across component re-mounts or page navigations,
//   and does NOT auto-refresh the access token before it expires (1 hour).
//
//   Symptom: After navigating between several pages, getSession() returns
//   null — the token expired and was never refreshed. All subsequent API
//   calls go out with no Authorization header. FastAPI returns 401 → the
//   browser console shows this as a 500 Internal Server Error.
//
// AFTER (fixed):
//   persistSession: true
//     Saves the session (access_token + refresh_token) to localStorage so
//     it survives component unmounts, re-mounts, and full page refreshes.
//     getSession() can restore it reliably on every call.
//
//   autoRefreshToken: true
//     The client silently calls the Supabase token endpoint ~60 seconds
//     before the access_token expires and swaps in the new token.
//     Without this, any navigation after ~1 hour results in a dead token
//     and every API call fails with 401/500 until the user logs in again.
//
//   detectSessionInUrl: true
//     Handles magic-link / OAuth callback flows where Supabase appends
//     the session to the URL fragment. Required for email verification
//     links to work correctly. Harmless when not using those flows.
//
// This matches exactly what rag-admin/src/supabase.js already does correctly.
// ─────────────────────────────────────────────────────────────────────────────

import { createClient } from '@supabase/supabase-js'
    
const supabaseUrl  = import.meta.env.VITE_SUPABASE_URL  || ''
const supabaseAnon = import.meta.env.VITE_SUPABASE_ANON_KEY || ''

if (!supabaseUrl || !supabaseAnon) {
  console.warn(
    '[supabase] VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY is not set. ' +
    'Auth will not work. Check your .env file.'
  )
}

export const supabase = createClient(supabaseUrl, supabaseAnon, {
  auth: {
    persistSession   : true,   // save session to localStorage → survives re-mounts
    autoRefreshToken : true,   // silently refresh token before 1-hour expiry
    detectSessionInUrl: true,  // handle magic-link / email verification callbacks
  },
})