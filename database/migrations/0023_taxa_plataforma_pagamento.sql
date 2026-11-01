-- Taxa da plataforma gravada em cada pagamento, porque ela depende do plano do profissional e o plano muda.
-- Nulo = pagamento anterior a esta migration, que continua a 10% (sem UPDATE).
ALTER TABLE PAGAMENTO ADD TAXA_PLATAFORMA_PERCENTUAL NUMERIC(5,2);
