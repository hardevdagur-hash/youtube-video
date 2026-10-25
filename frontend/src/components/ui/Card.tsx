import type { ReactNode, HTMLAttributes } from 'react';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  hover?: boolean;
  padding?: 'sm' | 'md' | 'lg';
  children: ReactNode;
}

const paddings = {
  sm: 'p-4',
  md: 'p-6',
  lg: 'p-6 sm:p-8',
};

export default function Card({
  hover = false,
  padding = 'md',
  children,
  className = '',
  ...props
}: CardProps) {
  return (
    <div
      className={`bg-white dark:bg-gray-900 rounded-2xl ${
        hover ? 'hover:shadow-card-hover transition-all-300' : ''
      } ${paddings[padding]} ${className}`}
      {...props}
    >
      {children}
    </div>
  );
}
