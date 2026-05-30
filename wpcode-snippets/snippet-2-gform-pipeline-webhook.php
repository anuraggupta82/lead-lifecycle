<?php
/**
 * WPCode Snippet — GDC: Gravity Forms → Lead Pipeline Webhook
 * Type:     PHP Snippet — Run Everywhere
 * Status:   Active
 *
 * PURPOSE:
 *   On every Gravity Form submission (Form 1 = GDC contact form), POSTs a
 *   lead_created event to the lead lifecycle pipeline at /api/events.
 *   Runs server-side (no CORS issues). Fires after form validation passes
 *   (entry already saved to GF), so data is complete.
 *
 *   NOTE: NxtSmile leads are handled separately by the nxtsmile.com backend.
 *   This snippet is only for graftondentalcare.com contact form submissions.
 *
 * FORM FIELD MAP — GDC Contact Form (Form ID 1):
 *   Field 1  — Full Name      (single text field)
 *   Field 2  — Email
 *   Field 3  — Phone
 *   Field 4  — Message / Goals
 *   Field 6  — Preferred Day  (checkbox — optional)
 *   Field 8  — Preferred Time (checkbox — optional)
 *   Field 10 — gclid          (hidden, filled by snippet-1 JS)
 *   Field 11 — utm_source     (hidden)
 *   Field 12 — utm_medium     (hidden)
 *   Field 13 — utm_campaign   (hidden)
 *   Field 14 — utm_term       (hidden)
 *   Field 15 — utm_content    (hidden)
 *   Field 16 — landing_url    (hidden)
 *   Field 17 — ga4_client_id  (hidden)
 *
 * PIPELINE URL: Change GDC_PIPELINE_URL if your backend port/host changes.
 *   For Cloud-hosted pipeline, replace with the Cloud Run URL.
 */

add_action( 'gform_after_submission_1', 'gdc_send_lead_to_pipeline', 10, 2 );

function gdc_send_lead_to_pipeline( $entry, $form ) {

    define( 'GDC_PIPELINE_URL', 'http://localhost:7070/api/events' );

    // ── Extract form fields ──────────────────────────────────────────────────

    // Field 1: Full Name (single text field — split on first space)
    $full_name  = isset( $entry['1'] ) ? sanitize_text_field( $entry['1'] ) : '';
    $name_parts = explode( ' ', trim( $full_name ), 2 );
    $first_name = $name_parts[0];
    $last_name  = isset( $name_parts[1] ) ? $name_parts[1] : '';

    // Field 2: Email
    $email = isset( $entry['2'] ) ? sanitize_email( $entry['2'] ) : '';

    // Field 3: Phone
    $phone = isset( $entry['3'] ) ? sanitize_text_field( $entry['3'] ) : '';

    // Field 4: Message / Goals
    $message = isset( $entry['4'] ) ? sanitize_textarea_field( $entry['4'] ) : '';

    // Field 6: Preferred Day (checkbox — GF stores checked values as '6.1', '6.2', etc.)
    $preferred_days = array();
    foreach ( $entry as $key => $val ) {
        if ( strpos( (string) $key, '6.' ) === 0 && ! empty( $val ) ) {
            $preferred_days[] = sanitize_text_field( $val );
        }
    }

    // Field 8: Preferred Time (checkbox)
    $preferred_times = array();
    foreach ( $entry as $key => $val ) {
        if ( strpos( (string) $key, '8.' ) === 0 && ! empty( $val ) ) {
            $preferred_times[] = sanitize_text_field( $val );
        }
    }

    // ── Attribution hidden fields (populated by snippet-1 JS) ───────────────
    $gclid         = isset( $entry['10'] ) ? sanitize_text_field( $entry['10'] ) : '';
    $utm_source    = isset( $entry['11'] ) ? sanitize_text_field( $entry['11'] ) : '';
    $utm_medium    = isset( $entry['12'] ) ? sanitize_text_field( $entry['12'] ) : '';
    $utm_campaign  = isset( $entry['13'] ) ? sanitize_text_field( $entry['13'] ) : '';
    $utm_term      = isset( $entry['14'] ) ? sanitize_text_field( $entry['14'] ) : '';
    $utm_content   = isset( $entry['15'] ) ? sanitize_text_field( $entry['15'] ) : '';
    $landing_url   = isset( $entry['16'] ) ? esc_url_raw( $entry['16'] )          : '';
    $ga4_client_id = isset( $entry['17'] ) ? sanitize_text_field( $entry['17'] ) : '';

    // Fallback: if JS didn't populate landing_url, use the URL GF captures natively
    if ( empty( $landing_url ) ) {
        $landing_url = isset( $entry['source_url'] ) ? esc_url_raw( $entry['source_url'] ) : '';
    }

    // ── Build goals array ────────────────────────────────────────────────────
    $goals = array();
    if ( ! empty( $message ) ) {
        $goals[] = $message;
    }
    if ( ! empty( $preferred_days ) ) {
        $goals[] = 'Preferred days: ' . implode( ', ', $preferred_days );
    }
    if ( ! empty( $preferred_times ) ) {
        $goals[] = 'Preferred times: ' . implode( ', ', $preferred_times );
    }

    // ── Build deterministic lead_id (lead_<sha256[:16]>) ────────────────────
    // Matches pipeline logic: primary key is email, fallback is phone digits only.
    $hash_input = ! empty( $email )
        ? strtolower( trim( $email ) )
        : preg_replace( '/\D/', '', $phone );
    $lead_id = 'lead_' . substr( hash( 'sha256', $hash_input ), 0, 16 );

    // ── Assemble payload ─────────────────────────────────────────────────────
    $payload = array(
        'event_type'    => 'lead_created',
        'lead_id'       => $lead_id,
        'source'        => 'gdc_contact_form',   // identifies graftondentalcare.com form
        'first_name'    => $first_name,
        'last_name'     => $last_name,
        'email'         => $email,
        'phone'         => $phone,
        'goals'         => $goals,
        'gclid'         => $gclid,
        'utm_source'    => $utm_source,
        'utm_medium'    => $utm_medium,
        'utm_campaign'  => $utm_campaign,
        'utm_term'      => $utm_term,
        'utm_content'   => $utm_content,
        'landing_url'   => $landing_url,
        'ga4_client_id' => $ga4_client_id,
        'created_at'    => date( 'c', strtotime( $entry['date_created'] ) ),
    );

    // ── POST to pipeline (fire-and-forget) ───────────────────────────────────
    $response = wp_remote_post(
        GDC_PIPELINE_URL,
        array(
            'headers'   => array( 'Content-Type' => 'application/json' ),
            'body'      => wp_json_encode( $payload ),
            'timeout'   => 10,      // seconds — pipeline is local, should be fast
            'blocking'  => false,   // don't slow the form confirmation page
            'sslverify' => false,   // localhost has no TLS cert
        )
    );

    // Log errors to PHP error log (visible in WP debug log if WP_DEBUG_LOG is on)
    if ( is_wp_error( $response ) ) {
        error_log( '[GDC Pipeline] wp_remote_post failed: ' . $response->get_error_message() );
    }
}
