<?php
/**
 * Plugin Name: WOD Import Helper
 * Description: Expose le CPT event, ses taxonomies et ses champs meta via WP REST API
 *              pour les scripts d'import automatique (daily_import.py, cc_import.py).
 * Version: 1.2
 */

// ── 1. Activer le CPT « event » en REST ─────────────────────────────────────
add_action('init', function () {
    global $wp_post_types;
    if (isset($wp_post_types['event'])) {
        $wp_post_types['event']->show_in_rest          = true;
        $wp_post_types['event']->rest_base             = 'events';
        $wp_post_types['event']->rest_controller_class = 'WP_REST_Posts_Controller';
    }
}, 20);

// ── 2. Activer les taxonomies en REST ───────────────────────────────────────
add_action('init', function () {
    global $wp_taxonomies;
    $map = [
        'type'      => 'event_type',
        'event_loc' => 'event_loc',
        'event_cat' => 'event_cat',
        'event_tag' => 'event_tag',
    ];
    foreach ($map as $slug => $rest_base) {
        if (isset($wp_taxonomies[$slug])) {
            $wp_taxonomies[$slug]->show_in_rest = true;
            $wp_taxonomies[$slug]->rest_base    = $rest_base;
        }
    }
}, 20);

// ── 3. Enregistrer les champs meta string avec show_in_rest ─────────────────
// Priorité 30 : après OVA (priority ~20) pour pouvoir unregister+re-register.
add_action('init', function () {
    $string_fields = [
        'ova_mb_event_start_date_str',
        'ova_mb_event_end_date_str',
        'ova_mb_event_address',
        'ova_mb_event_map_address',
        'ova_mb_event_map_lat',
        'ova_mb_event_map_lng',
        'ova_mb_event_ticket_external_link',
        'ova_mb_event_time_zone',
        'ova_mb_event_event_type',
        'ova_mb_event_info_organizer',
        'ova_mb_event_allow_cancellation_booking',
        'ova_mb_event_ticket_link',
        'ova_mb_event_option_calendar',
        'ova_mb_event_event_days',
        'ova_mb_event_min_price',
        'ova_mb_event_max_price',
        'ova_mb_event_price_desc',
        'ova_mb_event_ticket_external_link_price',
        'ova_mb_event_name_organizer',
        'ova_mb_event_phone_organizer',
        'ova_mb_event_mail_organizer',
    ];
    foreach ($string_fields as $key) {
        // Supprime l'éventuelle registration OVA (sans show_in_rest) avant la nôtre.
        unregister_post_meta('event', $key);
        register_post_meta('event', $key, [
            'show_in_rest'  => true,
            'single'        => true,
            'type'          => 'string',
            'auth_callback' => '__return_true',
        ]);
    }

    // Calendrier OVA : tableau de périodes
    unregister_post_meta('event', 'ova_mb_event_calendar');
    register_post_meta('event', 'ova_mb_event_calendar', [
        'show_in_rest' => [
            'schema' => [
                'type'  => 'array',
                'items' => [
                    'type'                 => 'object',
                    'additionalProperties' => true,
                    'properties'           => [
                        'calendar_id'         => ['type' => 'string'],
                        'date'                => ['type' => 'string'],
                        'end_date'            => ['type' => 'string'],
                        'start_time'          => ['type' => 'string'],
                        'end_time'            => ['type' => 'string'],
                        'book_before_minutes' => ['type' => 'string'],
                    ],
                ],
            ],
        ],
        'single'        => true,
        'type'          => 'array',
        'auth_callback' => '__return_true',
    ]);

    // Galerie EventList : tableau (vide par défaut). Sans ce champ initialisé,
    // eventlist/templates/loop/thumbnail.php fait array_unshift() sur une
    // chaîne vide renvoyée par get_post_meta() pour une clé jamais posée →
    // fatal error. Incident du 20/08/2026 (27 events HYROX créés sans cette
    // clé, page "événements liés" plantait en 500 sur tout le site).
    unregister_post_meta('event', 'ova_mb_event_gallery');
    register_post_meta('event', 'ova_mb_event_gallery', [
        'show_in_rest'  => [
            'schema' => ['type' => 'array', 'items' => ['type' => 'string']],
        ],
        'single'        => true,
        'type'          => 'array',
        'default'       => [],
        'auth_callback' => '__return_true',
    ]);
}, 30);

// ── 4. Fallback : sauvegarder les meta via update_post_meta après chaque REST ─
// Garantit la sauvegarde même si register_post_meta échoue silencieusement.
add_action('rest_after_insert_event', function ($post, $request, $creating) {
    $body = $request->get_json_params();
    $meta = isset($body['meta']) && is_array($body['meta']) ? $body['meta'] : [];
    if (empty($meta)) {
        return;
    }
    $allowed = [
        'ova_mb_event_start_date_str',
        'ova_mb_event_end_date_str',
        'ova_mb_event_address',
        'ova_mb_event_map_address',
        'ova_mb_event_map_lat',
        'ova_mb_event_map_lng',
        'ova_mb_event_ticket_external_link',
        'ova_mb_event_time_zone',
        'ova_mb_event_event_type',
        'ova_mb_event_info_organizer',
        'ova_mb_event_allow_cancellation_booking',
        'ova_mb_event_ticket_link',
        'ova_mb_event_option_calendar',
        'ova_mb_event_event_days',
        'ova_mb_event_min_price',
        'ova_mb_event_max_price',
        'ova_mb_event_price_desc',
        'ova_mb_event_ticket_external_link_price',
        'ova_mb_event_name_organizer',
        'ova_mb_event_phone_organizer',
        'ova_mb_event_mail_organizer',
        'ova_mb_event_calendar',
        'ova_mb_event_gallery',
    ];
    foreach ($allowed as $key) {
        if (array_key_exists($key, $meta)) {
            update_post_meta($post->ID, $key, $meta[$key]);
        }
    }
}, 10, 3);
