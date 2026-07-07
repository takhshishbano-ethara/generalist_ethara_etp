{
    'name': 'Ethara HRMS Extension',
    'version': '19.0.1.0.1',
    'summary': 'Extends hr.job and hr.applicant with Ethara AI job posting + application fields',
    'description': 'Adds job posting metadata (slug, work mode, salary, responsibilities, approval workflow, screening prompt, etc.) to hr.job and portfolio/github/resume URLs plus cancellation tracking to hr.applicant.',
    'category': 'Human Resources/Recruitment',
    'depends': ['base', 'hr', 'hr_recruitment', 'api_auth_gateway'],
    'data': [
        'security/ir.model.access.csv',
        'views/hr_job_views.xml',
        'views/ethara_candidate_views.xml',
        'views/hr_applicant_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
