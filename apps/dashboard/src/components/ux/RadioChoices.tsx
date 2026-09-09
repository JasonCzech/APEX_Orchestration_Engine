import { useId } from 'react'
export function RadioChoices<T extends string>({ label, value, options, onChange }: {
  label: string; value: T; options: readonly { id: T; label: string }[]; onChange: (value: T) => void
}) {
  const name = useId()
  return <fieldset className="radio-choices"><legend>{label}</legend>
    {options.map(option => <label key={option.id} className={value === option.id ? 'selected' : ''}>
      <input type="radio" name={name} value={option.id} checked={value === option.id} onChange={() => onChange(option.id)} />
      {option.label}
    </label>)}
  </fieldset>
}
