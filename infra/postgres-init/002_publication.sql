DO $block$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_publication
        WHERE pubname = 'cdc_pub'
    ) THEN
        CREATE PUBLICATION cdc_pub FOR TABLE public.products;
    END IF;
END
$block$;

