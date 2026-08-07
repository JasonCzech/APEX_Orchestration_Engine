import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { PipelineState } from '@apex/pipeline-events'

import { RunRail } from '../RunRail'

import { PIPELINE_DETAIL } from './testUtils'

describe('RunRail', () => {
  it('renders the safe native APEX Load console link when the engine provides one', () => {
    const consoleUrl = 'https://apex-load.example/#/test/native-run-1'
    const state = {
      artifacts: [],
      phase_results: {},
      engine_handle: {
        engine: 'apex_load',
        connection_id: 'connection-1',
        external_run_id: 'native-run-1',
        console_url: consoleUrl,
      },
    } as unknown as PipelineState

    render(
      <MemoryRouter>
        <RunRail detail={PIPELINE_DETAIL} state={state} interrupts={[]} />
      </MemoryRouter>,
    )

    expect(screen.getByRole('link', { name: 'Open in APEX Load' })).toHaveAttribute(
      'href',
      consoleUrl,
    )
  })
})
