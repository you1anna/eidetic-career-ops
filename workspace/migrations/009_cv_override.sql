-- CV choice is the owner's decision, separate from source facts and derived relevance.
ALTER TABLE records ADD COLUMN cv_override TEXT DEFAULT NULL
    CHECK (cv_override IS NULL OR (type = 'job' AND cv_override IN
        ('Director', 'Manager', 'QE', 'QELead', 'FDE', 'Founding')));
