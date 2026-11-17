import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cx } from "./util";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type Size = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: Size;
}

const VARIANT: Record<ButtonVariant, string | false> = { primary: "primary", secondary: false, ghost: "ghost", danger: "danger" };
const SIZE: Record<Size, string | false> = { sm: "btn-sm", md: false, lg: "btn-lg" };

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", className, type = "button", ...rest }, ref,
) {
  return <button ref={ref} type={type} className={cx(VARIANT[variant], SIZE[size], className)} {...rest} />;
});

export interface IconButtonProps extends Omit<ButtonProps, "children" | "aria-label"> {
  /** Required: icon-only buttons have no visible text. */
  label: string;
  children: React.ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, variant = "ghost", size = "md", className, children, type = "button", ...rest }, ref,
) {
  return (
    <button ref={ref} type={type} aria-label={label} title={label} className={cx("icon", VARIANT[variant], SIZE[size], className)} {...rest}>
      {children}
    </button>
  );
});
