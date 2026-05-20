<?php
/**
 * WPCode Snippet — GDC: Gravity Forms → Lead Pipeline Webhook
 * Type:     PHP Snippet — Run Everywhere
 * Status:   Active
 *
 * PURPOSE:
 *   On every Gravity Form 1 submission, POSTs a lead_created event to
 *   the local lead lifecycle pipeline at http://localhost:7070/api/events.
 *   Runs server-side so there's no CORS issue. Fires after form validation
 *   passes (entry already saved to GF), so the lead data is complete.
 *
 * FORM FIELD MAP (update if Gravity Forms field IDs change):
 *   Field 2    — First Name  (Name field, input_1_2_3 = first, input_1_2_6 = last)
 *   Field 3    — Email
 *   Field 4    — Phone
 *   Field 5    — Message
 *   Field 6    — Preferred Day  (checkbox)
 *   Field 7    — Preferred Time (checkbox)
 *   Field 10   — gclid          (hidden)
 *   Field 11   — utm_source     (hidden)
 *   Field 12   — utm_medium     (hidden)
 *   Field 13   — utm_campaign   (hidden)
 *   Field 14   — utm_term       (hidden)
 *   Field 15   — utm_content    (hidden)
 *   Field 16   — landing_url    (hidden)
 *   Field 17   — ga4_client_id  (hidden)
 *
 * NOTE: The Name field in Gravity Forms is a compound field. GF stores
 *   first name as input 2.3 and last name as input 2.6 in $entry.
 *   If your form uses a single "Full Name" text field, replace those
 *   with $entry['2'] and split on space.
 *
 * PIPELINE URL: Change PIPELINE_URL if your backend port changes.
 *   For Cloud-hosted pipeline, replace with the Cloud Run URL.
 */

add_action( 'gform_after_submission_1', 'gdc_send_lead_to_pipeline', 10, 2 );

function gdc_send_lead_to_pipeline( $entry, $form ) {

    define( 'GDC_PIPELINE_URL', 'http://localhost:7070/api/events' );

    // ── Extract form fields ──────────────────────────────────────────────────

    // Name field (GF compound field ID 2: first=2.3, last=2.6)
    $first_name = isset( $entry['2.3'] ) ? sanitize_text_field( $entry['2.3'] ) : '';
    $last_name  = isset( $entry['2.6'] ) ? sanitize_text_field( $entry['2.6'] ) : '';

    // If both are empty, fall back to a single Name field (ID 2)
    if ( empty( $first_name ) && ! empty( $entry['2'] ) ) {
        $parts      = explode( ' ', trim( $entry['2'] ), 2 );
        $first_name = $parts[0];
        $last_name  = isset( $parts[1] ) ? $parts[1] : '';
    }

    $email   = isset( $entry['3'] ) ? sanitize_email( $entry['3'] )         : '';
    $phone   = isset( $entry['4'] ) ? sanitize_text_field( $entry['4'] )    : '';
    $message = isset( $entry['5'] ) ? sanitize_textarea_field( $entry['5'] ): '';

    // Attribution hidden fields (populated by JS snippet 1)
    $gclid        = isset( $entry['10'] ) ? sanitize_text_field( $entry['10'] ) : '';
    $utm_source   = isset( $entry['11'] ) ? sanitize_text_field( $entry['11'] ) : '';
    $utm_medium   = isset( $entry['12'] ) ? sanitize_text_field( $entry['12'] ) : '';
    $utm_campaign = isset( $entry['13'] ) ? sanitize_text_field( $entry['13'] ) : '';
    $utm_term     = isset( $entry['14'] ) ? sanitize_text_field( $entry['14'] ) : '';
    $utm_content  = isset( $entry['15'] ) ? sanitize_text_field( $entry['15'] ) : '';
    $landing_url  = isset( $entry['16'] ) ? esc_url_raw( $entry['16'] )         : '';
    $ga4_client_id= isset( $entry['17'] ) ? sanitize_text_field( $entry['17'] ) : '';

    // Fallback: if JS didn't fill landing_url, use the source_url GF captures
    if ( empty( $landing_url ) ) {
        $landing_url = isset( $entry['source_url'] ) ? esc_url_raw( $entry['source_url'] ) : '';
    }

    // ── Build deterministic lead_id (matches pipeline logic: lead_<sha256[:16]>) ──

    $hash_input = ! empty( $email ) ? strtolower( trim( $email ) ) : preg_replace( '/\D/', '', $phone );
    $lead_id    = 'lead_' . substr( hash( 'sha256', $hash_input ), 0, 16 );

    // ── Build goals array from message / form fields ─────────────────────────
    $goals = array();
    if ( ! empty( $message ) ) {
        $goals[] = $message;
    }

    // ── Assemble payload ─────────────────────────────────────────────────────
    $payload = array(
        'event_type'   => 'lead_created',
        'lead_id'      => $lead_id,
        'source'       => 'contact_form',
        'first_name'   => $first_name,
        'last_name'    => $last_name,
        'email'        => $email,
        'phone'        => $phone,
        'goals'        => $goals,
        'gclid'        => $gclid,
        'utm_source'   => $utm_source,
        'utm_medium'   => $utm_medium,
        'utm_campaign' => $utm_campaign,
        'utm_term'     => $utm_term,
        'utm_content'  => $utm_content,
        'landing_url'  => $landing_url,
        'ga4_client_id'=> $ga4_client_id,
        'created_at'   => date( 'c', strtotime( $entry['date_created'] ) ),
    );

    // ── POST to pipeline ─────────────────────────────────────────────────────
    $response = wp_remote_post(
        GDC_PIPELINE_URL,
        array(
            'headers'     => array( 'Content-Type' => 'application/json' ),
            'body'        => wp_json_encode( $payload ),
            'timeout'     => 10,       // seconds — pipeline is local, should be fast
            'blocking'    => false,    // fire-and-forget: don't slow the form confirmation page
            'sslverify'   => false,    // localhost has no TLS cert
        )
    );

    // Log errors to PHP error log (visible in WP debug log if WP_DEBUG_LOG is on)
    if ( is_wp_error( $response ) ) {
        error_log( '[GDC Pipeline] wp_remote_post failed: ' . $response->get_error_message() );
    }
}
