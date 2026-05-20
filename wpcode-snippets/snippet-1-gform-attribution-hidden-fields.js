/**
 * WPCode Snippet — GDC: Gravity Forms Attribution Hidden Fields
 * Type:     JavaScript — Site Wide Footer
 * Status:   Active
 *
 * PURPOSE:
 *   Reads gclid / UTM params from the page URL and writes them into
 *   hidden Gravity Forms fields (IDs 10–17) so the PHP submission
 *   hook can read them and forward to the pipeline.
 *
 * SETUP IN GRAVITY FORMS:
 *   Add the following Hidden fields to Form 1 with these exact Field IDs:
 *     Field 10 — gclid
 *     Field 11 — utm_source
 *     Field 12 — utm_medium
 *     Field 13 — utm_campaign
 *     Field 14 — utm_term
 *     Field 15 — utm_content
 *     Field 16 — landing_url
 *     Field 17 — ga4_client_id
 *
 *   To set a custom Field ID in Gravity Forms:
 *   Edit the field → Advanced tab → "Field ID" → enter the number above.
 *
 * HOW IT WORKS:
 *   - Runs once on DOMContentLoaded
 *   - Reads URL params from the current page (gclid, utm_*, fbclid, msclkid)
 *   - Stores them in sessionStorage (persists if user navigates away and back)
 *   - Fills the hidden GF fields so the server-side PHP hook can read them
 *   - Also reads the GA4 _ga cookie for session stitching
 */
(function () {
  var SS_KEY = 'gdc_attribution';

  function readGa4ClientId() {
    try {
      var m = document.cookie.match(/_ga=GA1\.\d+\.(\d+\.\d+)/);
      return m ? m[1] : '';
    } catch (e) { return ''; }
  }

  function captureAttribution() {
    var qs = new URLSearchParams(window.location.search);
    var keys = ['gclid','fbclid','msclkid','gbraid','wbraid',
                'utm_source','utm_medium','utm_campaign','utm_term','utm_content'];
    var fresh = {};
    keys.forEach(function(k) {
      var v = qs.get(k);
      if (v) fresh[k] = v;
    });

    var existing = {};
    try {
      var raw = sessionStorage.getItem(SS_KEY);
      if (raw) existing = JSON.parse(raw);
    } catch(e) {}

    // First-touch wins
    var merged = Object.assign({}, fresh, existing);
    if (!existing.captured_at) {
      merged.landing_url = window.location.href;
      merged.captured_at = new Date().toISOString();
    }
    var cid = readGa4ClientId();
    if (cid) merged.ga4_client_id = cid;

    try { sessionStorage.setItem(SS_KEY, JSON.stringify(merged)); } catch(e) {}
    return merged;
  }

  function fillHiddenFields(attr) {
    var map = {
      'input_1_10': attr.gclid        || '',
      'input_1_11': attr.utm_source   || '',
      'input_1_12': attr.utm_medium   || '',
      'input_1_13': attr.utm_campaign || '',
      'input_1_14': attr.utm_term     || '',
      'input_1_15': attr.utm_content  || '',
      'input_1_16': attr.landing_url  || window.location.href,
      'input_1_17': attr.ga4_client_id|| '',
    };
    Object.keys(map).forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.value = map[id];
    });
  }

  function init() {
    var attr = captureAttribution();
    fillHiddenFields(attr);

    // Re-fill if GF re-renders the form (multi-page, conditional logic)
    if (typeof jQuery !== 'undefined') {
      jQuery(document).on('gform_page_loaded', function() {
        fillHiddenFields(captureAttribution());
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
