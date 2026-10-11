"""Explicit adapter for pre-existing recorded test answers, never production."""
from opportunity_agent.v2.research.extraction import AdmissionExtraction, AdmissionFact, degree_of
from opportunity_agent.v2.research.temporal import source_intake


def structured(context, facts):
    converted = []
    for item in facts:
        data = item.model_dump() if hasattr(item, 'model_dump') else dict(item)
        block = next((b for b in context['blocks'] if data['quote'] in b['text']), context['blocks'][0])
        scope = '\n'.join([*block['headings'],block['text']])
        intake = source_intake('',scope)
        # Existing fixtures explicitly use these source spellings; no production inference.
        quote = next((s for s in (intake, ' '.join(reversed(intake.split()))) if s and s in scope), '')
        title = context.get('title', block.get('title', ''))
        program = title if title.startswith('Master of ') or not context['program'] else context['program']
        required, considered = {'required':(True,None),'optional':(False,True),
            'not_required':(False,None),'not_accepted':(None,False)}.get(data['value'],(None,None))
        converted.append(AdmissionFact(**data,block_id=block['id'],university=context['university'],
            program=program,program_quote=program,degree=degree_of(program),
            scope='all',intake=intake,intake_quote=quote,
            gre_required=required,gre_considered=considered,
            date_text=data['quote'].split(':',1)[-1].strip().rstrip('.') if data['field']=='deadline' else '',
            deadline_kind='final' if data['field']=='deadline' else 'unknown'))
    return AdmissionExtraction(facts=converted)
