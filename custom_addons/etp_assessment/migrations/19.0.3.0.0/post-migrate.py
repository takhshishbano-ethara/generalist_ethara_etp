def migrate(cr, version):
    # 19.0.3.0.0: etp.assessment.day gained a Many2many skill_ids.
    # Backfill it from the legacy Many2one skill_id so existing rows
    # have a non-empty source pool after upgrade. skill_id then becomes
    # a stored compute pinned to skill_ids[:1] (zero edits at read sites).
    cr.execute(
        """
        INSERT INTO etp_assessment_day_skill_rel (day_id, skill_id)
        SELECT d.id, d.skill_id
          FROM etp_assessment_day d
         WHERE d.skill_id IS NOT NULL
           AND NOT EXISTS (
                 SELECT 1
                   FROM etp_assessment_day_skill_rel r
                  WHERE r.day_id = d.id
                    AND r.skill_id = d.skill_id
               );
        """
    )
