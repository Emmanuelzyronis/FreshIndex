CREATE TABLE IF NOT EXISTS public.products (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku text NOT NULL UNIQUE,
    name text NOT NULL,
    description text NOT NULL,
    category text NOT NULL,
    price_cents integer NOT NULL CHECK (price_cents >= 0),
    in_stock boolean NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE public.products REPLICA IDENTITY DEFAULT;

