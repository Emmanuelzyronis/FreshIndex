DO $block$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_replication_slots
        WHERE slot_name = 'cdc_products_slot'
    ) THEN
        PERFORM pg_catalog.pg_create_logical_replication_slot(
            'cdc_products_slot',
            'pgoutput'
        );
    END IF;
END
$block$;

